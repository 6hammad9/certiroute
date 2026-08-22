"""Route-first Streamlit interface for the CertiRoute hackathon product.

The default view is a crew hand-off: one decision, one numbered map, and one
ordered stop list. Model comparison, temperatures, and API provenance live in
a separate planner view so they remain auditable without crowding the route.
"""

from __future__ import annotations

import csv
import os
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import pydeck as pdk
import streamlit as st
from pydantic import ValidationError

from certiroute.collection import (
    CacheCorruptionError,
    HeatmapSnapshotStore,
    heatmap_request_fingerprint,
)
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    FortyGuardClient,
    polygon_area_square_miles,
)
from certiroute.fortyguard.errors import FortyGuardError
from certiroute.job_manifest import (
    MAX_MANIFEST_JOBS,
    MIN_MANIFEST_JOBS,
    REQUIRED_JOB_COLUMNS,
    JobManifest,
    JobManifestIssue,
    JobManifestValidation,
    validate_job_manifest,
)
from certiroute.optimization import (
    ConditionPoint,
    InfeasibleScheduleError,
    SchedulePlan,
    ScheduleSearchLimitError,
    ScheduleStrategy,
    TemperatureProfile,
    optimize_job_order,
)
from certiroute.real_conditions import (
    HeatmapCollectionPlan,
    RealTemperatureBatch,
    build_profile_requests,
    collect_real_temperature_batch_from_plan,
    plan_profile_collection,
)
from certiroute.risk import relative_exposure_reduction

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"

EXAMPLE_DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)
DEFAULT_REPLAY_DATE = date(2026, 7, 15)
EXAMPLE_SHIFT_START = time(8)
EXAMPLE_SHIFT_END = time(17)
GRANULARITY_METRES = 60
REFERENCE_TEMPERATURE_C = 27.0
PLANNING_THRESHOLD_C = 35.0
HEAT_WEIGHT = 8.0
AVERAGE_TRAVEL_SPEED_KPH = 25.0
MAX_UPLOAD_BYTES = 1024 * 1024
UPLOAD_STRING_COLUMNS = {
    "job_id": "string",
    "name": "string",
    "earliest_start": "string",
    "latest_finish": "string",
}
JOB_TEMPLATE_CSV = (
    "job_id,name,latitude,longitude,duration_minutes,priority,"
    "earliest_start,latest_finish\n"
    "WO-001,Replace with first work site,33.44855,-112.07391,45,5,08:00,12:00\n"
    "WO-002,Replace with second work site,33.44530,-112.06670,60,3,09:00,16:00\n"
).encode("utf-8-sig")
PLAN_LABELS = {
    ScheduleStrategy.EFFICIENCY: "Distance-efficient operations baseline",
    ScheduleStrategy.HEAT_AWARE: "Heat-aware recommendation",
}


@st.cache_data
def load_sample_jobs() -> pd.DataFrame:
    """Load the committed, non-sensitive demonstration work orders."""

    return pd.read_csv(SAMPLE_JOBS_PATH)


def parse_uploaded_jobs(payload: bytes) -> JobManifestValidation:
    """Parse one small UTF-8 CSV without persisting customer work orders."""

    if len(payload) > MAX_UPLOAD_BYTES:
        return JobManifestValidation(
            manifest=None,
            issues=(JobManifestIssue("CSV files must be 1 MB or smaller."),),
        )
    try:
        decoded = payload.decode("utf-8-sig")
        header = next(csv.reader(StringIO(decoded)), [])
        if len(header) != len(set(header)):
            return JobManifestValidation(
                manifest=None,
                issues=(
                    JobManifestIssue(
                        "Required column names cannot appear more than once.",
                        field="columns",
                    ),
                ),
            )
        frame = pd.read_csv(
            StringIO(decoded),
            dtype=UPLOAD_STRING_COLUMNS,
            keep_default_na=True,
        )
    except UnicodeDecodeError:
        return JobManifestValidation(
            manifest=None,
            issues=(JobManifestIssue("CSV must use UTF-8 text encoding."),),
        )
    except (csv.Error, pd.errors.EmptyDataError, pd.errors.ParserError, ValueError):
        return JobManifestValidation(
            manifest=None,
            issues=(JobManifestIssue("The CSV could not be read as a table."),),
        )
    return validate_job_manifest(frame)


def cache_path() -> Path:
    """Return an injectable cache root so tests and deployments stay isolated."""

    configured = os.getenv("CERTIROUTE_HEATMAP_CACHE_PATH")
    return Path(configured) if configured else DEFAULT_CACHE_PATH


def minutes_of_day(value: time) -> int:
    """Return the wall-clock minute used by the scheduler and API samples."""

    return value.hour * 60 + value.minute


def hourly_sample_times(shift_start: time, shift_end: time) -> tuple[time, ...]:
    """Cover a same-day shift at hourly intervals, including both boundaries."""

    start_minute = minutes_of_day(shift_start)
    end_minute = minutes_of_day(shift_end)
    if end_minute <= start_minute:
        raise ValueError("Shift finish must be later than shift start.")
    samples = list(range(start_minute, end_minute + 1, 60))
    if samples[-1] != end_minute:
        samples.append(end_minute)
    return tuple(time(minute // 60, minute % 60) for minute in samples)


def constant_profiles(
    jobs: list[Job], shift_start: time, shift_end: time
) -> dict[str, TemperatureProfile]:
    """Build neutral profiles used only to test route feasibility pre-network."""

    points = (
        ConditionPoint(
            minute_of_day=minutes_of_day(shift_start),
            temperature_c=REFERENCE_TEMPERATURE_C,
            certainty=1.0,
        ),
        ConditionPoint(
            minute_of_day=minutes_of_day(shift_end),
            temperature_c=REFERENCE_TEMPERATURE_C,
            certainty=1.0,
        ),
    )
    return {
        job.job_id: TemperatureProfile(job_id=job.job_id, points=points) for job in jobs
    }


def optimized_plans(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
) -> dict[ScheduleStrategy, SchedulePlan]:
    """Build only the two customer-facing plans from the same constraints."""

    common = {
        "jobs": jobs,
        "profiles": profiles,
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": AVERAGE_TRAVEL_SPEED_KPH,
        "reference_temperature_c": REFERENCE_TEMPERATURE_C,
        "planning_threshold_c": PLANNING_THRESHOLD_C,
        "uncertainty_penalty": 0.0,
        "heat_weight": HEAT_WEIGHT,
    }
    return {
        strategy: optimize_job_order(strategy=strategy, **common)
        for strategy in (
            ScheduleStrategy.EFFICIENCY,
            ScheduleStrategy.HEAT_AWARE,
        )
    }


def minute_label(minute_of_day: int | float) -> str:
    """Format minutes after midnight as a compact 24-hour time."""

    rounded = int(round(minute_of_day))
    hours, minutes = divmod(rounded, 60)
    return f"{hours:02d}:{minutes:02d}"


def api_key_available() -> bool:
    """Check configuration without rendering or exposing the secret."""

    try:
        settings = get_settings()
    except ValidationError:
        return False
    return bool(settings.fortyguard_api_key.get_secret_value().strip())


def inject_styles() -> None:
    """Add a small product layer without hiding native Streamlit behavior."""

    st.markdown(
        """
        <style>
        .stApp { background: #F6FAFB; }
        .block-container { max-width: 1180px; padding-top: 1.6rem; }
        h1 { letter-spacing: -0.045em; }
        h2, h3 { letter-spacing: -0.025em; }
        .hero-copy {
            color: #486581; font-size: 1.08rem; line-height: 1.55;
            max-width: 780px; margin-top: -0.45rem;
        }
        .hero-heading {
            color: #102A43; font-size: 1.55rem; font-weight: 750;
            letter-spacing: -0.025em; line-height: 1.3; margin: .8rem 0 1rem;
        }
        .eyebrow {
            color: #0B6B8A; font-size: .77rem; font-weight: 800;
            letter-spacing: .12em; text-transform: uppercase;
        }
        .badge {
            display: inline-block; border: 1px solid #B8D8E3;
            background: #EAF6F8; color: #0B5A73; border-radius: 999px;
            padding: .28rem .62rem; margin: .15rem .3rem .15rem 0;
            font-size: .72rem; font-weight: 750; letter-spacing: .035em;
        }
        .badge.heat-badge {
            border-color: #F2B38B; background: #FFF4EA; color: #B9380A;
        }
        .process-strip {
            display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;
            color: #486581; margin: 1rem 0 1.25rem;
        }
        .process-step {
            display: inline-flex; align-items: center; gap: .45rem;
            border: 1px solid #C8DCE3; background: white;
            border-radius: 999px; padding: .42rem .72rem; font-weight: 700;
        }
        .process-number {
            display: inline-flex; align-items: center; justify-content: center;
            width: 1.45rem; height: 1.45rem; border-radius: 50%;
            color: white; background: #0B6B8A; font-size: .78rem;
        }
        .process-arrow { color: #829AB1; font-weight: 800; }
        .empty-state {
            text-align: center; padding: 1.7rem 1rem; border: 1px dashed #9FB3C8;
            border-radius: .75rem; background: white;
        }
        .safety-note {
            border-left: 4px solid #C2410C; background: #FFF4EA;
            padding: .85rem 1rem; border-radius: .35rem; color: #B9380A;
        }
        .decision-card {
            border: 1px solid #B8D8E3; border-left: 6px solid #0B6B8A;
            background: #EAF6F8; border-radius: .8rem; padding: 1.15rem 1.25rem;
            margin: .65rem 0 1rem;
        }
        .decision-card h2 { margin: .15rem 0 .35rem; color: #102A43; }
        .decision-card p { margin: 0; color: #486581; line-height: 1.5; }
        .decision-label {
            color: #0B6B8A; font-size: .72rem; font-weight: 850;
            letter-spacing: .11em; text-transform: uppercase;
        }
        .route-summary {
            display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: .65rem; margin: .8rem 0 1.2rem;
        }
        .route-fact {
            background: white; border: 1px solid #C8DCE3; border-radius: .7rem;
            padding: .78rem .9rem; min-width: 0;
        }
        .route-fact-label {
            color: #486581; font-size: .7rem; font-weight: 800;
            letter-spacing: .08em; text-transform: uppercase;
        }
        .route-fact-value {
            color: #102A43; font-size: 1.08rem; font-weight: 800;
            margin-top: .12rem; overflow-wrap: anywhere;
        }
        .route-stop {
            display: grid; grid-template-columns: 2.65rem minmax(0, 1fr) auto;
            gap: .72rem; align-items: center; background: white;
            border: 1px solid #C8DCE3; border-radius: .72rem;
            padding: .7rem .78rem; margin-bottom: .5rem; min-width: 0;
        }
        .route-stop-number {
            display: flex; align-items: center; justify-content: center;
            width: 2.35rem; height: 2.35rem; border-radius: 50%;
            color: white; background: #C2410C; font-weight: 850; font-size: 1rem;
        }
        .route-stop-copy { min-width: 0; }
        .route-stop-kicker {
            color: #0B6B8A; font-size: .66rem; font-weight: 850;
            letter-spacing: .08em; text-transform: uppercase;
        }
        .route-stop-name {
            color: #102A43; font-weight: 800; line-height: 1.25;
            overflow-wrap: anywhere;
        }
        .route-stop-task {
            color: #486581; font-size: .78rem; line-height: 1.25;
            overflow-wrap: anywhere; margin-top: .12rem;
        }
        .route-stop-time {
            color: #243B53; font-weight: 800; white-space: nowrap;
            text-align: right;
        }
        .route-stop-travel {
            color: #486581; font-size: .72rem; margin-top: .12rem;
        }
        .route-return {
            border: 1px dashed #9FB3C8; border-radius: .65rem;
            color: #486581; padding: .62rem .78rem; margin-top: .2rem;
            background: white; overflow-wrap: anywhere;
        }
        .map-note {
            color: #486581; font-size: .78rem; line-height: 1.4;
            margin: .15rem 0 .55rem;
        }
        div[data-testid="stMetric"] {
            background: white; border: 1px solid #C8DCE3;
            padding: .85rem 1rem; border-radius: .7rem;
        }
        button[kind="primary"] {
            background: #0B6B8A; border-color: #0B6B8A;
        }
        button[kind="primary"]:hover {
            background: #07546A; border-color: #07546A;
        }
        @media (max-width: 700px) {
            .block-container { padding-left: 1rem; padding-right: 1rem; }
            .process-arrow { display: none; }
            .route-summary { grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .route-fact:first-child { grid-column: 1 / -1; }
            .route-stop { grid-template-columns: 2.65rem minmax(0, 1fr); }
            .route-stop-time {
                grid-column: 2; text-align: left; white-space: normal;
            }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_hero() -> None:
    """Explain the customer, decision, and evidence before any controls."""

    st.markdown(
        '<div class="eyebrow">Heat-aware field operations</div>',
        unsafe_allow_html=True,
    )
    st.title("CertiRoute")
    st.markdown(
        """
        <div class="hero-heading">
          Turn real field jobs into one heat-aware crew route.
        </div>
        <div class="hero-copy">
        Upload a compact U.S. workday. CertiRoute searches feasible stop orders
        against real FortyGuard temperature intelligence and returns one
        crew-ready route.
        </div>
        <div class="hero-badges" style="margin-top:.7rem">
          <span class="badge heat-badge">REAL FORTYGUARD DATA</span>
          <span class="badge">YOUR WORK ORDERS</span>
          <span class="badge">HISTORICAL REPLAY</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_result_mode_styles() -> None:
    """Collapse onboarding copy once the customer has a finished route."""

    st.markdown(
        """
        <style>
        .hero-heading, .hero-copy, .hero-badges, .process-strip { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_three_steps() -> None:
    """Give first-time visitors a compact three-step mental model."""

    st.markdown(
        """
        <div class="process-strip" aria-label="How CertiRoute works">
          <span class="process-step">
            <span class="process-number">1</span>Add work orders
          </span>
          <span class="process-arrow">→</span>
          <span class="process-step">
            <span class="process-number">2</span>Confirm shift
          </span>
          <span class="process-arrow">→</span>
          <span class="process-step">
            <span class="process-number">3</span>Build crew route
          </span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def timing_change(start_minute: int, baseline_start: int) -> str:
    """Translate a start-time delta into dispatcher-friendly language."""

    difference = start_minute - baseline_start
    if difference == 0:
        return "No change"
    hours, minutes = divmod(abs(difference), 60)
    amount = f"{hours}h {minutes}m" if hours else f"{minutes}m"
    return f"{amount} {'later' if difference > 0 else 'earlier'}"


def recommended_sequence_rows(
    baseline: SchedulePlan, recommendation: SchedulePlan
) -> pd.DataFrame:
    """Build the primary dispatcher hand-off table."""

    baseline_by_job = {stop.job_id: stop for stop in baseline.stops}
    return pd.DataFrame(
        [
            {
                "Stop": stop.sequence,
                "Work order": stop.job_id,
                "Site and task": stop.job_name,
                "Start": minute_label(stop.start_minute),
                "Finish": minute_label(stop.finish_minute),
                "Ambient temperature": f"{stop.temperature_c:.1f} °C",
                "Change from baseline": timing_change(
                    stop.start_minute,
                    baseline_by_job[stop.job_id].start_minute,
                ),
            }
            for stop in recommendation.stops
        ]
    )


def site_and_task(job_name: str) -> tuple[str, str]:
    """Split the demonstration label into concise crew-facing copy."""

    site, separator, task = job_name.partition(" — ")
    if not separator:
        return job_name, "Scheduled field work"
    return site, task.removeprefix("demo ").capitalize()


def google_maps_route_url(plan: SchedulePlan, depot: GeoPoint) -> str:
    """Build a Google Maps directions URL that preserves the planned order."""

    coordinates = [f"{stop.latitude:.6f},{stop.longitude:.6f}" for stop in plan.stops]
    depot_coordinates = f"{depot.latitude:.6f},{depot.longitude:.6f}"
    return "https://www.google.com/maps/dir/?" + urlencode(
        {
            "api": "1",
            "origin": depot_coordinates,
            "destination": depot_coordinates,
            "travelmode": "driving",
            "waypoints": "|".join(coordinates),
        }
    )


def safe_spreadsheet_text(value: str) -> str:
    """Prevent uploaded text from becoming a spreadsheet formula on export."""

    return f"'{value}" if value.lstrip().startswith(("=", "+", "-", "@")) else value


def crew_route_csv(plan: SchedulePlan) -> bytes:
    """Export a crew run sheet with the planned sequence and coordinates."""

    rows = [
        {
            "stop": stop.sequence,
            "work_order": safe_spreadsheet_text(stop.job_id),
            "site_and_task": safe_spreadsheet_text(stop.job_name),
            "latitude": stop.latitude,
            "longitude": stop.longitude,
            "arrive": minute_label(stop.arrival_minute),
            "start": minute_label(stop.start_minute),
            "finish": minute_label(stop.finish_minute),
            "estimated_travel_from_previous_min": stop.inbound_travel_minutes,
        }
        for stop in plan.stops
    ]
    return pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig")


def render_route(plan: SchedulePlan, depot: GeoPoint) -> None:
    """Render a fitted, numbered visit sequence that is readable without hover."""

    route = [
        [depot.longitude, depot.latitude],
        *[[stop.longitude, stop.latitude] for stop in plan.stops],
        [depot.longitude, depot.latitude],
    ]
    stops = [
        {
            "position": [stop.longitude, stop.latitude],
            "sequence": stop.sequence,
            "marker": str(stop.sequence),
            "job_id": stop.job_id,
            "site": escape(site_and_task(stop.job_name)[0]),
            "time": (
                f"{minute_label(stop.start_minute)}–{minute_label(stop.finish_minute)}"
            ),
        }
        for stop in plan.stops
    ]
    fitted = pdk.data_utils.compute_view(route)
    view = pdk.ViewState(
        latitude=fitted.latitude,
        longitude=fitted.longitude,
        zoom=max(1.0, float(fitted.zoom) - 0.85),
        pitch=0,
        bearing=0,
    )
    layers = [
        pdk.Layer(
            "PathLayer",
            [{"path": route}],
            get_path="path",
            get_color=[255, 255, 255, 235],
            get_width=10,
            width_units="'pixels'",
        ),
        pdk.Layer(
            "PathLayer",
            [{"path": route}],
            get_path="path",
            get_color=[11, 107, 138, 255],
            get_width=5,
            width_units="'pixels'",
        ),
        pdk.Layer(
            "ScatterplotLayer",
            [{"position": [depot.longitude, depot.latitude], "label": "Depot"}],
            get_position="position",
            get_radius=24,
            radius_units="'pixels'",
            get_fill_color=[16, 42, 67, 255],
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            stops,
            get_position="position",
            get_radius=17,
            radius_units="'pixels'",
            get_fill_color=[194, 65, 12, 255],
            stroked=True,
            get_line_color=[255, 255, 255, 255],
            get_line_width=2,
            line_width_units="'pixels'",
            pickable=True,
        ),
        pdk.Layer(
            "TextLayer",
            stops,
            get_position="position",
            get_text="marker",
            get_size=15,
            size_units="'pixels'",
            get_color=[255, 255, 255, 255],
            get_text_anchor="'middle'",
            get_alignment_baseline="'center'",
            billboard=True,
        ),
        pdk.Layer(
            "TextLayer",
            [
                {
                    "position": [depot.longitude, depot.latitude],
                    "label": "DEPOT · START / FINISH",
                }
            ],
            get_position="position",
            get_text="label",
            get_size=12,
            size_units="'pixels'",
            get_color=[16, 42, 67, 255],
            get_pixel_offset=[0, -45],
            get_text_anchor="'middle'",
            get_alignment_baseline="'center'",
            background=True,
            get_background_color=[255, 255, 255, 235],
            background_padding=[5, 3],
            billboard=True,
        ),
    ]
    st.pydeck_chart(
        pdk.Deck(
            layers=layers,
            initial_view_state=view,
            map_style=pdk.map_styles.CARTO_LIGHT,
            tooltip={
                "html": "<b>Stop {sequence}: {site}</b><br/>{time}",
                "style": {"backgroundColor": "#102A43", "color": "white"},
            },
        ),
        width="stretch",
        height=475,
        key="crew-route-map",
        on_select="ignore",
    )


def render_crew_itinerary(plan: SchedulePlan) -> None:
    """List only the instructions a crew needs to follow the sequence."""

    cards: list[str] = []
    for stop in plan.stops:
        site, task = site_and_task(stop.job_name)
        instruction = "Start here" if stop.sequence == 1 else "Next stop"
        travel = (
            "Depot and first site share this location"
            if stop.sequence == 1 and stop.inbound_travel_minutes == 0
            else f"{stop.inbound_travel_minutes} min estimated travel from prior stop"
        )
        cards.append(
            f"""
            <div class="route-stop" data-route-stop="{stop.sequence}">
              <div class="route-stop-number">{stop.sequence}</div>
              <div class="route-stop-copy">
                <div class="route-stop-kicker">{instruction}</div>
                <div class="route-stop-name">{escape(site)}</div>
                <div class="route-stop-task">{escape(task)}</div>
                <div class="route-stop-travel">{escape(travel)}</div>
              </div>
              <div class="route-stop-time">
                {minute_label(stop.start_minute)}–{minute_label(stop.finish_minute)}
              </div>
            </div>
            """
        )
    inbound = sum(stop.inbound_travel_minutes for stop in plan.stops)
    return_minutes = max(0, plan.total_travel_minutes - inbound)
    cards.append(
        '<div class="route-return"><strong>Return to depot</strong><br>'
        f"{return_minutes} min estimated travel · back by "
        f"<strong>{minute_label(plan.route_finish_minute)}</strong></div>"
    )
    st.markdown("".join(cards), unsafe_allow_html=True)


def render_run_sheet(plan: SchedulePlan, depot: GeoPoint) -> None:
    """Render the operator-facing route, itinerary, and hand-off actions."""

    st.markdown("### Follow this route")
    st.caption(
        "The large numbers are the visit order. Stop 1 is where the shift begins."
    )
    directions_column, download_column = st.columns(2)
    with directions_column:
        st.link_button(
            "Open ordered stops in Google Maps",
            google_maps_route_url(plan, depot),
            width="stretch",
        )
    with download_column:
        st.download_button(
            "Download crew route (CSV)",
            data=crew_route_csv(plan),
            file_name="certiroute-crew-route.csv",
            mime="text/csv",
            width="stretch",
        )
    st.caption(
        "Google Maps provides road directions for the same ordered stops; desktop "
        "is recommended because mobile browsers may accept fewer waypoints."
    )
    map_column, list_column = st.columns([1.45, 1], gap="large")
    with map_column:
        render_route(plan, depot)
        st.markdown(
            '<div class="map-note">Order line only—not turn-by-turn navigation. '
            "Travel times are straight-line estimates at 25 km/h.</div>",
            unsafe_allow_html=True,
        )
    with list_column:
        render_crew_itinerary(plan)


def render_metrics(
    baseline: SchedulePlan,
    recommendation: SchedulePlan,
    reduction: float | None,
) -> None:
    """Show the benefit, operational cost, and completeness in four cards."""

    exposure_value = (
        "No change"
        if reduction is None or abs(reduction) < 0.005
        else f"{reduction:.1%} lower"
    )
    threshold_change = (
        recommendation.minutes_above_planning_threshold
        - baseline.minutes_above_planning_threshold
    )
    extra_travel = recommendation.total_travel_minutes - baseline.total_travel_minutes
    columns = st.columns(4)
    columns[0].metric(
        "Modeled exposure",
        exposure_value,
        help=(
            "Degree-hours above 27 °C integrated across exact work minutes. "
            "This is a planning comparison, not a medical risk score."
        ),
    )
    columns[1].metric(
        "Hot-work time ≥35 °C",
        f"{recommendation.minutes_above_planning_threshold:.0f} min",
        delta=(
            None
            if abs(threshold_change) < 0.05
            else f"{threshold_change:+.0f} min vs baseline"
        ),
        delta_color="inverse",
        help="A configurable comparison line, not a regulatory limit.",
    )
    columns[2].metric(
        "Added estimated travel",
        f"{extra_travel:+d} min",
        help="Estimated from straight-line distance at 25 km/h, not road navigation.",
    )
    columns[3].metric(
        "Jobs completed on time",
        f"{len(recommendation.stops)} / {len(baseline.stops)}",
        help="Every job remains inside its configured time window.",
    )


def render_crew_decision(
    baseline: SchedulePlan,
    recommendation: SchedulePlan,
    reduction: float | None,
) -> SchedulePlan:
    """State one decision and return the plan the crew should actually follow."""

    order_changed = [stop.job_id for stop in baseline.stops] != [
        stop.job_id for stop in recommendation.stops
    ]
    use_heat_order = order_changed and reduction is not None and reduction >= 0.005
    crew_plan = recommendation if use_heat_order else baseline
    first_site = escape(site_and_task(crew_plan.stops[0].job_name)[0])

    if use_heat_order:
        extra_travel = (
            recommendation.total_travel_minutes - baseline.total_travel_minutes
        )
        travel_copy = (
            "without adding estimated travel"
            if extra_travel <= 0
            else f"with {extra_travel} added estimated travel minutes"
        )
        decision_label = "Heat-aware route"
        title = "Use this stop order"
        explanation = (
            f"This order reduces modeled heat exposure by {reduction:.1%} "
            f"{travel_copy} and keeps every job on time. Start at "
            f"<strong>{first_site}</strong> at "
            f"<strong>{minute_label(crew_plan.stops[0].start_minute)}</strong>."
        )
    else:
        decision_label = "Balanced route"
        title = "Use this stop order"
        explanation = (
            "This route balances estimated travel, priorities, and time windows. "
            "The heat-aware search found no meaningful exposure reduction that "
            "justified a different sequence. Start at "
            f"<strong>{first_site}</strong> at "
            f"<strong>{minute_label(crew_plan.stops[0].start_minute)}</strong>."
        )

    st.markdown(
        f"""
        <div class="decision-card">
          <div class="decision-label">{decision_label}</div>
          <h2>{title}</h2>
          <p>{explanation}</p>
        </div>
        <div class="route-summary">
          <div class="route-fact">
            <div class="route-fact-label">First stop</div>
            <div class="route-fact-value">1 · {first_site}</div>
          </div>
          <div class="route-fact">
            <div class="route-fact-label">Shift route</div>
            <div class="route-fact-value">
              {minute_label(crew_plan.stops[0].start_minute)} →
              {minute_label(crew_plan.route_finish_minute)}
            </div>
          </div>
          <div class="route-fact">
            <div class="route-fact-label">Jobs on time</div>
            <div class="route-fact-value">
              {len(crew_plan.stops)} of {len(baseline.stops)}
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    return crew_plan


def render_planner_panel(
    batch: RealTemperatureBatch,
    jobs_frame: pd.DataFrame,
    plans: dict[ScheduleStrategy, SchedulePlan],
    reduction: float | None,
    *,
    is_example: bool,
) -> None:
    """Render the dispatcher/admin evidence without leaking it into crew view."""

    baseline = plans[ScheduleStrategy.EFFICIENCY]
    recommendation = plans[ScheduleStrategy.HEAT_AWARE]
    schedules_match = [stop.job_id for stop in baseline.stops] == [
        stop.job_id for stop in recommendation.stops
    ]
    st.markdown("### Planner details")
    st.caption(
        "For dispatchers, reviewers, and judges: methods, exact temperatures, "
        "constraints, and source records."
    )
    if schedules_match:
        st.info(
            "The operations baseline and heat-aware method chose the same route "
            "for this replay. There is no second route to compare visually."
        )
    render_metrics(baseline, recommendation, reduction)
    st.markdown("#### Scheduled stops and exact modeled conditions")
    st.dataframe(
        recommended_sequence_rows(baseline, recommendation),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": st.column_config.NumberColumn(width="small"),
            "Work order": st.column_config.TextColumn(width="small"),
            "Site and task": st.column_config.TextColumn(width="large"),
        },
    )
    render_detail_sections(batch, jobs_frame, plans, is_example=is_example)


def render_result(
    batch: RealTemperatureBatch,
    manifest: JobManifest,
    *,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
    is_example: bool,
) -> None:
    """Turn a completed batch into separate crew and planner experiences."""

    jobs = list(manifest.jobs)
    try:
        plans = optimized_plans(
            jobs,
            batch.profiles,
            depot=depot,
            shift_start=shift_start,
            shift_end=shift_end,
        )
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        st.error(
            "CertiRoute could not produce a complete route while keeping "
            f"every constraint: {exc}"
        )
        return

    baseline = plans[ScheduleStrategy.EFFICIENCY]
    recommendation = plans[ScheduleStrategy.HEAT_AWARE]
    reduction = relative_exposure_reduction(
        baseline.total_raw_exposure_units,
        recommendation.total_raw_exposure_units,
    )

    st.markdown("## Route result")
    view = st.segmented_control(
        "Result view",
        options=["Crew route", "Planner details"],
        default="Crew route",
        key="view_mode",
        label_visibility="collapsed",
    )
    if view == "Planner details":
        render_planner_panel(
            batch,
            manifest.frame,
            plans,
            reduction,
            is_example=is_example,
        )
        return

    crew_plan = render_crew_decision(baseline, recommendation, reduction)
    render_run_sheet(crew_plan, depot)
    render_safety_boundary(compact=True)


def render_detail_sections(
    batch: RealTemperatureBatch,
    jobs_frame: pd.DataFrame,
    plans: dict[ScheduleStrategy, SchedulePlan],
    *,
    is_example: bool,
) -> None:
    """Keep inputs, provenance, implementation detail, and caveats secondary."""

    job_label = (
        f"Review the {len(jobs_frame)} example work orders"
        if is_example
        else f"Review the {len(jobs_frame)} uploaded work orders"
    )
    with st.expander(job_label):
        display = jobs_frame.rename(
            columns={
                "job_id": "Work order",
                "name": "Site and task",
                "duration_minutes": "Minutes on site",
                "priority": "Priority (5 highest)",
                "earliest_start": "Not before",
                "latest_finish": "Finish by",
            }
        )
        st.dataframe(
            display[
                [
                    "Work order",
                    "Site and task",
                    "Minutes on site",
                    "Priority (5 highest)",
                    "Not before",
                    "Finish by",
                ]
            ],
            hide_index=True,
            width="stretch",
        )
        if is_example:
            st.caption(
                "Phoenix landmarks are real. Tasks, priorities, durations, "
                "windows, and service points are fictional example inputs."
            )
        else:
            st.caption(
                "These values came from the uploaded manifest. Confirm them "
                "against the source work-order system before dispatch."
            )
        st.map(jobs_frame[["latitude", "longitude"]], zoom=11)

    with st.expander("Verify the FortyGuard temperature evidence"):
        st.markdown(
            f"**Source:** FortyGuard Temperature API  ·  **Replay date:** "
            f"{batch.target_date.isoformat()}  ·  **Samples:** "
            f"{len(batch.samples)} hourly heatmaps  ·  **Tile setting:** "
            f"{batch.granularity} m"
        )
        temperatures = pd.DataFrame(
            [
                {
                    "Work order": job_id,
                    **{
                        minute_label(point.minute_of_day): point.temperature_c
                        for point in profile.points
                    },
                }
                for job_id, profile in batch.profiles.items()
            ]
        )
        st.dataframe(temperatures, hide_index=True, width="stretch")
        st.caption(
            "Temperatures between hourly samples are linearly interpolated. "
            "No synthetic or substitute temperature profile is generated."
        )
        st.markdown("**Source records**")
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Requested hour": minute_label(sample.minute_of_day),
                        "FortyGuard activity ID": sample.activity_id,
                        "Collected (UTC)": sample.collected_at_utc,
                        "Retrieved": (
                            "Saved API response" if sample.cache_hit else "This run"
                        ),
                    }
                    for sample in batch.samples
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(batch.request_time_assumption)

    with st.expander("Compare planning methods"):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Plan": PLAN_LABELS[strategy],
                        "What it balances": (
                            "Estimated travel and priority delay; heat ignored"
                            if strategy is ScheduleStrategy.EFFICIENCY
                            else (
                                "Estimated travel, priority delay, and modeled exposure"
                            )
                        ),
                        "Order": " → ".join(
                            stop.job_id for stop in plans[strategy].stops
                        ),
                        "Estimated travel": (
                            f"{plans[strategy].total_travel_minutes} min"
                        ),
                        "Modeled exposure": (
                            f"{plans[strategy].total_raw_exposure_units:.2f} "
                            "degree-hours"
                        ),
                        "Back at depot": minute_label(
                            plans[strategy].route_finish_minute
                        ),
                    }
                    for strategy in (
                        ScheduleStrategy.EFFICIENCY,
                        ScheduleStrategy.HEAT_AWARE,
                    )
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.markdown(
            f"The heat-aware score values one degree-hour above "
            f"{REFERENCE_TEMPERATURE_C:.0f} °C like {HEAT_WEIGHT:.0f} minutes of "
            "estimated travel, while retaining the same priority-delay term. "
            "That preference should become customer-configurable in production."
        )

    with st.expander("How the score works—and what comes next"):
        st.markdown(
            """
            **Modeled ambient-heat load** is the exact time integral of degrees
            above 27 °C while the crew is on site. For example, one hour at
            37 °C contributes 10 degree-hours. Lower is better.

            The scheduler evaluates feasible job orders, including travel,
            waiting, job windows, priorities, and the temperature throughout
            each work interval. The operations baseline ignores heat; the
            recommendation adds modeled exposure to the same operational score.

            **Research layer, not claimed in this result:** future work will
            calibrate forecast reliability from historical forecast-versus-
            realization error, then plan more conservatively under distribution
            shift. Until that evidence exists, this real-data page does not show
            or imply a certainty score.
            """
        )
        render_safety_boundary(compact=False)


def render_empty_state(
    *,
    job_count: int,
    missing_count: int,
    request_count: int,
    key_available: bool,
) -> None:
    """Explain exactly what the primary action will produce."""

    st.markdown(
        f"""
        <div class="empty-state">
          <h3 style="margin-top:0">Your numbered crew route will appear here</h3>
          <p>Build the route to turn these {job_count} jobs into one clear
          decision, a numbered map, an ordered stop list, and a depot return
          time.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if missing_count and not key_available:
        st.error(
            "FortyGuard connection required. Add `FORTYGUARD_API_KEY` to `.env` "
            "and reload. No schedule is calculated without real temperature evidence."
        )
    elif missing_count:
        st.info(
            f"Ready to build. {request_count - missing_count} of {request_count} "
            "temperature snapshots are already saved; "
            f"{missing_count} will be retrieved from FortyGuard."
        )
    else:
        st.info(
            f"Ready to build. All {request_count} required FortyGuard temperature "
            "snapshots are already saved."
        )


def render_safety_boundary(*, compact: bool) -> None:
    """Keep responsible-use limits visible at the right level of detail."""

    if compact:
        copy = (
            "<strong>Planning aid—not safety clearance.</strong> Confirm live site "
            "conditions and follow your heat-safety policy before dispatch."
        )
    else:
        copy = (
            "<strong>Planning support, not safety clearance.</strong> This "
            "prototype models ambient temperature only. It does not account for "
            "humidity, radiant heat, workload, clothing or PPE, acclimatization, "
            "or worker health. The 35 °C comparison line is configurable and is "
            "not a regulatory limit. It does not determine that work is safe."
        )
    st.markdown(
        f'<div class="safety-note">{copy}</div>',
        unsafe_allow_html=True,
    )


def render_job_source() -> tuple[JobManifest | None, bool]:
    """Collect and validate a real manifest, with the example kept secondary."""

    st.markdown("## 1. Add work orders")
    source = st.segmented_control(
        "Work-order source",
        options=["Upload my jobs", "Use Phoenix example"],
        default="Upload my jobs",
        key="job_source",
        help=(
            "Uploaded work orders are used only for this app session and are "
            "not written to project storage."
        ),
    )

    if source == "Use Phoenix example":
        validation = validate_job_manifest(load_sample_jobs())
        if not validation.is_valid or validation.manifest is None:
            st.error("The bundled Phoenix example failed validation.")
            return None, True
        manifest = validation.manifest
        st.info(
            "Example mode: Phoenix landmarks are real, but the work orders and "
            "constraints are fictional. Route temperatures still come only from "
            "FortyGuard."
        )
        with st.expander("Preview the Phoenix example jobs"):
            st.dataframe(manifest.frame, hide_index=True, width="stretch")
        return manifest, True

    upload_column, template_column = st.columns([1.8, 1])
    with upload_column:
        uploaded = st.file_uploader(
            "Upload work orders (CSV)",
            type=["csv"],
            key="job_manifest_upload",
            help=(
                f"UTF-8 CSV, 1 MB maximum, with {MIN_MANIFEST_JOBS}–"
                f"{MAX_MANIFEST_JOBS} U.S. work sites."
            ),
        )
    with template_column:
        st.markdown("**New to the format?**")
        st.download_button(
            "Download CSV template",
            data=JOB_TEMPLATE_CSV,
            file_name="certiroute-work-orders-template.csv",
            mime="text/csv",
            width="stretch",
        )
        st.caption("Replace both example rows with your own work orders.")

    if uploaded is None:
        st.info(
            "Start with the template, add 2–9 work sites, then upload it here. "
            "Required fields: ID, site name, coordinates, duration, priority, "
            "earliest start, and latest finish."
        )
        with st.expander("See the eight required CSV columns"):
            st.code(",".join(REQUIRED_JOB_COLUMNS), language="text")
            st.markdown(
                "Times use 24-hour `HH:MM`. Priority is 1–5. Coordinates must "
                "be U.S. latitude/longitude values supported by FortyGuard."
            )
        return None, False

    validation = parse_uploaded_jobs(uploaded.getvalue())
    if not validation.is_valid or validation.manifest is None:
        st.error("This work-order file is not ready yet.")
        for message in validation.error_messages or (
            "Check that the file is a UTF-8 CSV using the downloadable template.",
        ):
            st.markdown(f"- {escape(message)}")
        return None, False

    manifest = validation.manifest
    st.success(
        f"{len(manifest.jobs)} work orders validated. No API request has been made."
    )
    with st.expander("Review uploaded jobs"):
        st.dataframe(
            manifest.frame,
            hide_index=True,
            width="stretch",
            column_config={
                "job_id": st.column_config.TextColumn("Work order", width="small"),
                "name": st.column_config.TextColumn("Site and task", width="large"),
                "duration_minutes": st.column_config.NumberColumn(
                    "Duration (min)", width="small"
                ),
                "priority": st.column_config.NumberColumn("Priority", width="small"),
                "earliest_start": st.column_config.TextColumn(
                    "Not before", width="small"
                ),
                "latest_finish": st.column_config.TextColumn(
                    "Finish by", width="small"
                ),
            },
        )
        st.caption(
            "Check names, coordinates, durations, priorities, and time windows "
            "against your source system before continuing."
        )
        st.map(manifest.frame[["latitude", "longitude"]])
    return manifest, False


def preflight_route(
    jobs: list[Job],
    *,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
) -> None:
    """Prove that at least one complete route exists before any API submission."""

    optimize_job_order(
        jobs,
        constant_profiles(jobs, shift_start, shift_end),
        strategy=ScheduleStrategy.EFFICIENCY,
        depot=depot,
        shift_start=shift_start,
        shift_end=shift_end,
        average_travel_speed_kph=AVERAGE_TRAVEL_SPEED_KPH,
        reference_temperature_c=REFERENCE_TEMPERATURE_C,
        planning_threshold_c=PLANNING_THRESHOLD_C,
        uncertainty_penalty=0.0,
        heat_weight=HEAT_WEIGHT,
    )


def collect_batch(
    jobs: list[Job],
    plan: HeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
) -> RealTemperatureBatch:
    """Collect the exact missing set, or rebuild profiles entirely from cache."""

    if plan.new_task_count == 0:
        return collect_real_temperature_batch_from_plan(
            jobs,
            plan,
            store,
            client=None,
            max_new_tasks=0,
            now_utc=datetime.now(UTC),
        )

    settings = get_settings()
    with FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    ) as client:
        return collect_real_temperature_batch_from_plan(
            jobs,
            plan,
            store,
            client=client,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
            max_new_tasks=plan.new_task_count,
            now_utc=datetime.now(UTC),
        )


st.set_page_config(page_title="CertiRoute", page_icon="🌡️", layout="wide")
inject_styles()
render_hero()
render_three_steps()
route_result_slot = st.container()

manifest, is_example = render_job_source()
if manifest is None:
    st.markdown("---")
    render_safety_boundary(compact=True)
    st.stop()

domain_jobs = list(manifest.jobs)
source_token = "example" if is_example else "upload"
scenario_token = f"{source_token}_{manifest.fingerprint[:12]}"
default_shift_start = time.fromisoformat(str(manifest.frame["earliest_start"].min()))
default_shift_end = time.fromisoformat(str(manifest.frame["latest_finish"].max()))
default_depot = EXAMPLE_DEPOT if is_example else domain_jobs[0].location
yesterday = date.today() - timedelta(days=1)
default_date = min(DEFAULT_REPLAY_DATE, yesterday) if is_example else yesterday

st.markdown("## 2. Confirm the shift")
with st.container(border=True):
    schedule_column, depot_column = st.columns(2, gap="large")
    with schedule_column:
        st.markdown("**When does this crew work?**")
        selected_date = st.date_input(
            "Historical replay date",
            value=default_date,
            min_value=date(2021, 1, 1),
            max_value=yesterday,
            key=f"replay_date_{scenario_token}",
            help=(
                "This version validates a completed workday. Current-day and "
                "forecast routing will be added after the API time-zone contract "
                "is confirmed."
            ),
        )
        shift_start = st.time_input(
            "Shift starts",
            value=(EXAMPLE_SHIFT_START if is_example else default_shift_start),
            step=timedelta(minutes=15),
            key=f"shift_start_{scenario_token}",
        )
        shift_end = st.time_input(
            "Shift finishes",
            value=EXAMPLE_SHIFT_END if is_example else default_shift_end,
            step=timedelta(minutes=15),
            key=f"shift_end_{scenario_token}",
        )
    with depot_column:
        st.markdown("**Where does the crew start and finish?**")
        depot_latitude = st.number_input(
            "Depot latitude",
            min_value=-90.0,
            max_value=90.0,
            value=float(default_depot.latitude),
            step=0.0001,
            format="%.6f",
            key=f"depot_latitude_{scenario_token}",
        )
        depot_longitude = st.number_input(
            "Depot longitude",
            min_value=-180.0,
            max_value=180.0,
            value=float(default_depot.longitude),
            step=0.0001,
            format="%.6f",
            key=f"depot_longitude_{scenario_token}",
        )
        st.caption(
            "For uploads, the first job is only a starting suggestion. Replace "
            "it with the crew's actual U.S. depot."
        )

shift_duration_minutes = minutes_of_day(shift_end) - minutes_of_day(shift_start)
if shift_duration_minutes <= 0:
    st.error("Shift finish must be later than shift start on the same day.")
    render_safety_boundary(compact=True)
    st.stop()
if shift_duration_minutes > 12 * 60:
    st.error("Keep this planning shift to 12 hours or less.")
    render_safety_boundary(compact=True)
    st.stop()

depot = GeoPoint(latitude=depot_latitude, longitude=depot_longitude)
sample_times = hourly_sample_times(shift_start, shift_end)
requests = build_profile_requests(
    domain_jobs,
    target_date=selected_date,
    sample_times=sample_times,
    granularity=GRANULARITY_METRES,
)
area = polygon_area_square_miles(next(iter(requests.values())).polygon_aoi)
oversized = area > DEFAULT_MAX_AOI_AREA_SQUARE_MILES
store = HeatmapSnapshotStore(cache_path())
try:
    with st.spinner("Checking saved FortyGuard evidence…"):
        collection_plan = plan_profile_collection(
            requests,
            store,
            now_utc=datetime.now(UTC),
        )
except CacheCorruptionError as exc:
    st.error(f"Saved FortyGuard evidence failed its integrity check: {exc}")
    st.stop()

key_available = api_key_available()
st.markdown("## 3. Build the crew route")
with st.container(border=True):
    st.markdown(
        f"""
        <div class="route-summary">
          <div class="route-fact">
            <div class="route-fact-label">Work orders</div>
            <div class="route-fact-value">{len(domain_jobs)} validated</div>
          </div>
          <div class="route-fact">
            <div class="route-fact-label">Service area</div>
            <div class="route-fact-value">{area:.2f} mi²</div>
          </div>
          <div class="route-fact">
            <div class="route-fact-label">Temperature evidence</div>
            <div class="route-fact-value">
              {collection_plan.cache_hit_count} / {collection_plan.request_count} ready
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    build_clicked = st.button(
        "Build heat-aware route",
        type="primary",
        width="stretch",
        disabled=(
            oversized or (collection_plan.new_task_count > 0 and not key_available)
        ),
    )
    st.caption(
        "First CertiRoute proves the full shift is feasible. Then it loads only "
        "the missing real FortyGuard temperature snapshots—never substitutes."
    )

if oversized:
    st.error(
        f"These jobs span {area:.2f} mi², above the current "
        f"{DEFAULT_MAX_AOI_AREA_SQUARE_MILES:.0f} mi² FortyGuard request limit. "
        "Upload one compact service zone for this route."
    )
elif collection_plan.new_task_count > 0 and not key_available:
    st.error(
        "FortyGuard connection required. Add `FORTYGUARD_API_KEY` to `.env` "
        "and reload. No substitute temperature data will be generated."
    )

scenario_key = (
    str(store.root),
    source_token,
    manifest.fingerprint,
    depot.latitude,
    depot.longitude,
    shift_start.isoformat(),
    shift_end.isoformat(),
    selected_date.isoformat(),
    GRANULARITY_METRES,
    *(heatmap_request_fingerprint(request) for request in requests.values()),
)
if st.session_state.get("certiroute_active_scenario") != scenario_key:
    st.session_state["certiroute_active_scenario"] = scenario_key
    st.session_state.pop("view_mode", None)

batch: RealTemperatureBatch | None = None
if st.session_state.get("certiroute_result_scenario") == scenario_key:
    saved_batch = st.session_state.get("certiroute_temperature_batch")
    if isinstance(saved_batch, RealTemperatureBatch):
        batch = saved_batch

if build_clicked and not oversized:
    try:
        with st.status("Building a heat-aware workday…", expanded=True) as status:
            st.write(
                f"✓ Validated {len(domain_jobs)} work orders, their windows, "
                "and the depot return"
            )
            preflight_route(
                domain_jobs,
                depot=depot,
                shift_start=shift_start,
                shift_end=shift_end,
            )
            st.write("✓ Confirmed at least one complete route fits this shift")
            progress = st.progress(
                collection_plan.cache_hit_count / collection_plan.request_count,
                text=(
                    "Loading real FortyGuard temperatures · "
                    f"{collection_plan.cache_hit_count} of "
                    f"{collection_plan.request_count} already saved"
                ),
            )
            batch = collect_batch(domain_jobs, collection_plan, store)
            progress.progress(
                1.0,
                text=(
                    f"Loaded {collection_plan.request_count} of "
                    f"{collection_plan.request_count} temperature snapshots"
                ),
            )
            st.write("✓ Matched every work site to its returned temperature tile")
            st.write("✓ Compared feasible stop orders and preserved every window")
            status.update(label="Crew route ready", state="complete")
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        batch = None
        st.error(
            "No complete depot-to-depot route fits these job windows and shift. "
            "Adjust the depot, shift, durations, or time windows, then try again. "
            f"No FortyGuard request was submitted. Details: {exc}"
        )
    except (CacheCorruptionError, FortyGuardError, ValidationError, ValueError) as exc:
        batch = None
        st.error(f"The temperature lookup could not be completed: {exc}")
        st.info(
            "Any completed API responses were saved. Retry to continue from the "
            "remaining snapshots; no substitute data will be used."
        )

if batch is not None:
    st.session_state["certiroute_result_scenario"] = scenario_key
    st.session_state["certiroute_temperature_batch"] = batch
    with route_result_slot:
        render_result_mode_styles()
        render_result(
            batch,
            manifest,
            depot=depot,
            shift_start=shift_start,
            shift_end=shift_end,
            is_example=is_example,
        )
else:
    render_empty_state(
        job_count=len(domain_jobs),
        missing_count=collection_plan.new_task_count,
        request_count=collection_plan.request_count,
        key_available=key_available,
    )
    st.markdown("---")
    render_safety_boundary(compact=True)
