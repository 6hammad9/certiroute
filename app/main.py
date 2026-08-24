"""Route-first Streamlit interface for the CertiRoute hackathon product.

The default view is a crew hand-off: one decision, one numbered map, and one
ordered stop list. Model comparison, temperatures, and API provenance live in
a separate planner view so they remain auditable without crowding the route.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta
from html import escape
from io import StringIO
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import pydeck as pdk
import streamlit as st
from pydantic import ValidationError

import certiroute.map_picker as map_picker
from certiroute.animation import (
    HEAT_COLOR,
    ROUTE_COLOR,
    PlaybackRun,
    route_playback_html,
    route_playback_html_from_payload,
)
from certiroute.climatology import (
    DEFAULT_TRAINED_RADIUS_KM,
    ClimatologyUnavailableError,
    DiurnalClimatology,
    OutsideTrainedAreaError,
    load_climatology,
)
from certiroute.collection import (
    CacheCorruptionError,
    HeatmapSnapshotStore,
    heatmap_request_fingerprint,
)
from certiroute.config import get_settings
from certiroute.daily_level import (
    DailyLevelPendingError,
    DailyLevelReading,
    DailyLevelUnavailableError,
    collect_clustered_daily_level,
)
from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import InsufficientHistoryError
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.errors import FortyGuardError
from certiroute.fortyguard.heatmap_profiles import HeatmapCoverageError
from certiroute.job_manifest import (
    MAX_MANIFEST_JOBS,
    MIN_MANIFEST_JOBS,
    JobManifest,
    JobManifestIssue,
    JobManifestValidation,
    validate_job_manifest,
)
from certiroute.map_scenario import (
    DEFAULT_JOB_DURATION_MINUTES,
    DEFAULT_OPERATING_AREA_ID,
    DEFAULT_SHIFT_END,
    DEFAULT_SHIFT_START,
    OPERATING_AREA_PRESETS,
    MapClickAction,
    MapPoint,
    MapScenarioState,
    apply_map_click,
    build_default_job_manifest,
    select_operating_area,
    undo_last_point,
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
    ClusteredHeatmapCollectionPlan,
    RealTemperatureBatch,
    build_clustered_profile_requests,
    collect_clustered_real_temperature_batch_from_plan,
    plan_clustered_profile_collection,
)
from certiroute.risk import relative_exposure_reduction
from certiroute.same_day import (
    LeakageError,
    PlanningCoverageError,
    SameDayPlan,
    build_same_day_plan,
    score_plan_against_measurements,
)
from certiroute.shift_timing import ProfileCoverageError
from certiroute.showcase import (
    DEFAULT_EVIDENCE_ROOT,
    DEFAULT_SHOWCASE_PATH,
    load_grade_summary,
    load_graded_day,
)
from certiroute.theme import RESULT_MODE_STYLES, STYLESHEET, icon

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
DEFAULT_CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"
CLIMATOLOGY_ROOT = PROJECT_ROOT / "data" / "climatology"

EXAMPLE_DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)
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


def climatology_root() -> Path:
    """Return an injectable model root so tests and deployments stay isolated."""

    configured = os.getenv("CERTIROUTE_CLIMATOLOGY_PATH")
    return Path(configured) if configured else CLIMATOLOGY_ROOT


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


def step_done(message: str) -> None:
    """One completed step in a progress log, marked with a drawn tick."""

    st.markdown(
        '<div class="step-done">'
        f'{icon("check", size=15)}<span>{escape(message)}</span></div>',
        unsafe_allow_html=True,
    )


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
    """Apply the shared visual system defined in app/theme.py."""

    st.markdown(STYLESHEET, unsafe_allow_html=True)


def render_hero() -> None:
    """State the premise, the promise, and what backs it, in one band.

    Streamlit wraps each markdown call in its own element, so the whole band
    is emitted at once; split across calls the container would not enclose
    anything. The pitch sits on dark ground and the tool below it stays light,
    because a dispatcher works in the interface but does not have to be sold
    to in the same register as the controls.
    """

    st.markdown(
        f"""
        <div class="hero-band">
          <div class="wordmark">
            {icon("sunrise", size=20)}<span>CertiRoute</span>
            <span class="wordmark-tag">{icon("thermometer", size=13)}
              Heat-aware field operations</span>
          </div>
          <h1 class="hero-heading">
            Start the shift before the heat does.
          </h1>
          <div class="hero-copy">
          Outdoor crews work the hottest hours of the day by default, because
          the shift was set long before anyone knew what the day would do.
          CertiRoute reads today's street-level heat from FortyGuard, predicts
          the hours ahead, and tells you what time to begin &mdash; with the
          visit order already worked out.
          </div>
          <div class="hero-lede">
            Moving the shift is worth more than reordering the stops. That is
            measured, not assumed.
          </div>
          <div class="hero-proof">
            <span class="heat">{icon("thermometer", size=15)}
              Today's real measurements</span>
            <span>{icon("gauge", size=15)} Calibrated interval</span>
            <span>{icon("shield", size=15)} No synthetic fallback</span>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def section(number: int, title: str, *, blurb: str = "") -> None:
    """A numbered section heading, so the page reads as a sequence."""

    st.markdown(
        f'<div class="section-head"><span class="section-index">'
        f"[{number:02d}]</span><h2>{escape(title)}</h2>"
        + (f'<p>{escape(blurb)}</p>' if blurb else "")
        + "</div>",
        unsafe_allow_html=True,
    )


def render_landing_proof() -> None:
    """Show the product working before the visitor has done anything.

    A landing page that only describes a product asks to be believed. This
    plays a day the model had never seen, beside the record of every day it
    has been graded on - both read from committed files, so neither can drift
    away from what was actually measured.
    """

    graded = load_graded_day(PROJECT_ROOT / DEFAULT_SHOWCASE_PATH)
    summary = load_grade_summary(PROJECT_ROOT / DEFAULT_EVIDENCE_ROOT)
    if graded is None:
        return

    earlier = graded.home_earlier_minutes
    facts: list[tuple[str, str, str]] = []
    if graded.exposure_reduction is not None:
        facts.append(
            (
                "gauge",
                f"{graded.exposure_reduction:.0%}",
                "less heat exposure, same jobs",
            )
        )
    if earlier:
        facts.append(
            ("clock", f"{earlier // 60}h earlier", "home, on a day it never saw")
        )
    if summary is not None and summary.chose_best_every_time:
        facts.append(
            (
                "check",
                f"{summary.picked_best_start}/{summary.graded_days}",
                f"best possible start, across {len(summary.areas)} cities",
            )
        )

    st.markdown(
        '<div class="proof-head"><div>'
        f'<div class="proof-kicker">{icon("calendar", size=13)}'
        f"{escape(graded.area_label)} &middot; "
        f"{graded.day:%d %B %Y}</div>"
        '<div class="proof-title">A day CertiRoute had never seen</div>'
        "</div>"
        f'<div class="proof-note">It read one measurement &mdash; '
        f"{graded.measured_level_c:.1f} &deg;C across the area &mdash; then "
        f"sent the crew at {graded.recommended_start} instead of "
        f"{graded.baseline_start}. Watch both.</div></div>",
        unsafe_allow_html=True,
    )
    st.iframe(route_playback_html_from_payload(graded.payload), height=600)
    if facts:
        st.markdown(
            '<div class="proof-facts">'
            + "".join(
                f'<div class="proof-fact">{icon(name, size=16)}'
                f'<div><div class="proof-figure">{escape(figure)}</div>'
                f'<div class="proof-caption">{escape(caption)}</div></div></div>'
                for name, figure, caption in facts
            )
            + "</div>",
            unsafe_allow_html=True,
        )


def render_result_mode_styles() -> None:
    """Collapse onboarding copy once the customer has a finished route."""

    st.markdown(RESULT_MODE_STYLES, unsafe_allow_html=True)


def render_three_steps(state: MapScenarioState, *, planned: bool) -> None:
    """A guide that tracks real progress rather than describing the ideal path.

    A static three-step strip tells a first-time user what the product does but
    never what to do *now*. Each step here reports whether it is finished, in
    progress, or still ahead, and carries a hover note explaining it, so the
    guide keeps working after the first minute.
    """

    depot_done = state.depot is not None
    sites_done = state.job_count >= MIN_MANIFEST_JOBS
    steps = (
        (
            "map",
            "Place the crew base",
            depot_done,
            "Click once on the map. This is where the shift starts and ends.",
        ),
        (
            "pin",
            "Add work sites",
            sites_done,
            f"Click each place the crew must visit - at least "
            f"{MIN_MANIFEST_JOBS}, up to {MAX_MANIFEST_JOBS}.",
        ),
        (
            "sunrise",
            "Plan the shift",
            planned,
            "CertiRoute reads today's heat and picks the coolest start time "
            "that still fits every job.",
        ),
    )
    active_index = next(
        (index for index, step in enumerate(steps) if not step[2]), len(steps)
    )

    arrow = f'<span class="process-arrow">{icon("arrow-right", size=15)}</span>'
    rendered: list[str] = []
    for index, (name, text, done, hint) in enumerate(steps):
        if done:
            status, mark = "done", icon("check", size=13)
        elif index == active_index:
            status, mark = "active", str(index + 1)
        else:
            status, mark = "pending", str(index + 1)
        rendered.append(
            f'<span class="process-step {status}" title="{escape(hint)}">'
            f'<span class="process-number">{mark}</span>'
            f"{icon(name, size=15)}{escape(text)}</span>"
        )
    st.markdown(
        '<div class="process-strip" aria-label="Setup progress">'
        + arrow.join(rendered)
        + "</div>",
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
            get_color=[11, 21, 36, 235],
            get_width=10,
            width_units="'pixels'",
        ),
        pdk.Layer(
            "PathLayer",
            [{"path": route}],
            get_path="path",
            get_color=[112, 255, 210, 255],
            get_width=5,
            width_units="'pixels'",
        ),
        pdk.Layer(
            "ScatterplotLayer",
            [{"position": [depot.longitude, depot.latitude], "label": "Depot"}],
            get_position="position",
            get_radius=24,
            radius_units="'pixels'",
            get_fill_color=[112, 255, 210, 255],
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            stops,
            get_position="position",
            get_radius=17,
            radius_units="'pixels'",
            get_fill_color=[255, 145, 55, 255],
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
            get_color=[11, 21, 36, 255],
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
            get_color=[11, 21, 36, 255],
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
                "style": {"backgroundColor": "#0B1524", "color": "#F2F7FC"},
            },
        ),
        width="stretch",
        height=475,
        key="crew-route-map",
        on_select="ignore",
    )


def render_crew_itinerary(plan: SchedulePlan) -> None:
    """List only the instructions a crew needs to follow the sequence."""

    cards: list[str] = [
        (
            '<div class="route-endpoint">'
            '<div class="route-endpoint-node"></div>'
            "<div>"
            '<div class="route-stop-kicker">Start</div>'
            '<div class="route-stop-name">Crew base</div>'
            "</div>"
            "</div>"
        )
    ]
    for stop in plan.stops:
        site, task = site_and_task(stop.job_name)
        instruction = "Start here" if stop.sequence == 1 else "Next stop"
        travel = (
            "Depot and first site share this location"
            if stop.sequence == 1 and stop.inbound_travel_minutes == 0
            else f"{stop.inbound_travel_minutes} min estimated travel from prior stop"
        )
        cards.append(
            f'<div class="route-stop" data-route-stop="{stop.sequence}">'
            f'<div class="route-stop-number">{stop.sequence}</div>'
            '<div class="route-stop-copy">'
            f'<div class="route-stop-kicker">{instruction}</div>'
            f'<div class="route-stop-name">{escape(site)}</div>'
            f'<div class="route-stop-task">{escape(task)}</div>'
            f'<div class="route-stop-travel">{escape(travel)}</div>'
            "</div>"
            '<div class="route-stop-time">'
            f"{minute_label(stop.start_minute)}&ndash;"
            f"{minute_label(stop.finish_minute)}"
            "</div>"
            "</div>"
        )
    inbound = sum(stop.inbound_travel_minutes for stop in plan.stops)
    return_minutes = max(0, plan.total_travel_minutes - inbound)
    cards.append(
        '<div class="route-return"><div class="route-return-node"></div><div>'
        '<div class="route-stop-kicker">Finish</div>'
        '<div class="route-stop-name">Return to crew base</div>'
        f'<div class="route-stop-travel">{return_minutes} min '
        "estimated travel &middot; "
        f"back by <strong>{minute_label(plan.route_finish_minute)}</strong>"
        "</div></div></div>"
    )
    st.markdown(
        '<div class="route-rail">' + "".join(cards) + "</div>",
        unsafe_allow_html=True,
    )


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
        decision_label = "Heat-aware order"
        title = "The order that ran coolest"
        explanation = (
            f"On this day's measured temperatures, this order avoided "
            f"{reduction:.1%} of modelled heat exposure {travel_copy} while "
            "keeping every job on time. It begins at "
            f"<strong>{first_site}</strong> at "
            f"<strong>{minute_label(crew_plan.stops[0].start_minute)}</strong>."
        )
    else:
        decision_label = "Efficient order"
        title = "Reordering would not have helped"
        explanation = (
            "On this day's measured temperatures the heat-aware search found no "
            "sequence worth changing, so the efficient order stands. It begins "
            f"at <strong>{first_site}</strong> at "
            f"<strong>{minute_label(crew_plan.stops[0].start_minute)}</strong>."
        )

    st.markdown(
        f"""
        <div class="bento">
          <div class="bento-hero decision-card">
            <div class="decision-label">
              {icon("route", size=13)}{decision_label}
            </div>
            <h2>{title}</h2>
            <p>{explanation}</p>
          </div>
          <div class="bento-tile route-fact stop">
            <div class="route-fact-label">{icon("pin", size=13)}First stop</div>
            <div class="route-fact-value">1 · {first_site}</div>
          </div>
          <div class="bento-tile route-fact time">
            <div class="route-fact-label">{icon("clock", size=13)}Shift route</div>
            <div class="route-fact-value">
              {minute_label(crew_plan.stops[0].start_minute)} →
              {minute_label(crew_plan.route_finish_minute)}
            </div>
          </div>
          <div class="bento-tile route-fact status">
            <div class="route-fact-label">
              {icon("check", size=13)}Jobs on time
            </div>
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
    model: DiurnalClimatology | None = None,
    target_date: date | None = None,
) -> None:
    """Review a finished day: what was measured, and how the model scored."""

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

    section(3, "Reviewing a finished day")
    st.caption(
        "Every temperature below was measured by FortyGuard on this date, so "
        "the route and the grading can both be checked against evidence."
    )
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

    if target_date is not None:
        render_hindsight(
            jobs,
            batch,
            model,
            depot=depot,
            shift_start=shift_start,
            shift_end=shift_end,
            target_date=target_date,
        )
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
            f"{len(batch.samples)} heatmaps across {batch.aoi_count} "
            f"{'area' if batch.aoi_count == 1 else 'areas'}  ·  **Tile setting:** "
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
                        "Heat-data area": sample.aoi_index + 1,
                        "Requested hour": minute_label(sample.minute_of_day),
                        "Work orders covered": ", ".join(sample.job_ids),
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
) -> None:
    """Keep the pre-result state focused on the outcome, not API mechanics."""

    st.markdown(
        f"""
        <div class="empty-state">
          <h3 style="margin-top:0">Your start time and route will appear here</h3>
          <p>CertiRoute will read today's street-level heat around these
          {job_count} jobs, compare every start time this shift could use, and
          show the crew when to begin and where to go first.</p>
        </div>
        """,
        unsafe_allow_html=True,
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


MAP_SCENARIO_KEY = "certiroute_map_scenario"
MAP_GENERATION_KEY = "certiroute_map_generation"
SCENARIO_SOURCE_KEY = "certiroute_scenario_source"
SOURCE_MANIFEST_KEY = "certiroute_source_manifest"


def load_map_scenario() -> MapScenarioState:
    """Restore the current map choices without trusting corrupt session values."""

    saved = st.session_state.get(MAP_SCENARIO_KEY)
    if isinstance(saved, MapScenarioState):
        return saved
    if isinstance(saved, Mapping):
        try:
            return MapScenarioState.from_dict(saved)
        except ValueError:
            pass
    state = MapScenarioState()
    st.session_state[MAP_SCENARIO_KEY] = state.to_dict()
    st.session_state.setdefault(MAP_GENERATION_KEY, 0)
    st.session_state.setdefault(SCENARIO_SOURCE_KEY, "map")
    return state


def invalidate_route_result() -> None:
    """Prevent any changed selection from displaying a stale route."""

    for key in (
        "certiroute_active_scenario",
        "certiroute_result_scenario",
        "certiroute_temperature_batch",
        "view_mode",
    ):
        st.session_state.pop(key, None)


def save_map_scenario(
    state: MapScenarioState,
    *,
    source: str | None = None,
    source_manifest: JobManifest | None = None,
) -> None:
    """Persist map choices and invalidate results that no longer match."""

    st.session_state[MAP_SCENARIO_KEY] = state.to_dict()
    if source is not None:
        st.session_state[SCENARIO_SOURCE_KEY] = source
    if source_manifest is None:
        st.session_state.pop(SOURCE_MANIFEST_KEY, None)
    else:
        st.session_state[SOURCE_MANIFEST_KEY] = source_manifest
    invalidate_route_result()


def start_fresh_map(state: MapScenarioState) -> None:
    """Clear all points and remount the picker at the selected city."""

    fresh = MapScenarioState(operating_area_id=state.operating_area_id)
    save_map_scenario(fresh, source="map")
    st.session_state[MAP_GENERATION_KEY] = (
        int(st.session_state.get(MAP_GENERATION_KEY, 0)) + 1
    )


def source_manifest() -> JobManifest | None:
    """Return a session manifest only when it matches a supported source."""

    value = st.session_state.get(SOURCE_MANIFEST_KEY)
    return value if isinstance(value, JobManifest) else None


def render_picker_instruction(state: MapScenarioState) -> None:
    """Tell the user exactly what their next map tap will do."""

    if state.depot is None:
        if state.job_count:
            title = "Click where the crew starts and returns"
            detail = (
                f"Your {state.job_count} imported work sites are already orange. "
                "One map click places the mint crew base."
            )
        else:
            title = "First, click where the crew starts and returns"
            detail = "Pan or zoom if needed. Your first click becomes the mint base."
        style = ""
    elif state.job_count < MIN_MANIFEST_JOBS:
        remaining = MIN_MANIFEST_JOBS - state.job_count
        title = "Now click each place the crew needs to visit"
        detail = (
            f"Add {remaining} more orange work "
            f"{'site' if remaining == 1 else 'sites'}. Pan the map whenever you need."
        )
        style = ""
    else:
        title = f"Ready — {state.job_count} work sites selected"
        detail = "Add another site, adjust the optional details, or build the route."
        style = " ready"
    coverage = coverage_for(state.operating_area_id)
    if coverage is not None:
        detail += (
            f" The dashed circle is where CertiRoute's {escape(coverage.label)} "
            f"heat model applies — {coverage.radius_km:.0f} km across its "
            "trained ground."
        )
    st.markdown(
        f'<div class="picker-instruction{style}"><strong>{title}</strong>'
        f"<span>{detail}</span></div>",
        unsafe_allow_html=True,
    )


def render_selection_summary(
    state: MapScenarioState, manifest_hint: JobManifest | None
) -> None:
    """Render one continuous start-to-stops-to-return journey."""

    rows: list[str] = []
    depot_class = (
        "journey-node depot"
        if state.depot is not None
        else ("journey-node depot pending")
    )
    depot_title = (
        "Crew start &amp; return" if state.depot is not None else "Place the crew base"
    )
    depot_meta = (
        "Mint marker selected"
        if state.depot is not None
        else "Your first map click starts the route"
    )
    rows.append(
        '<div class="journey-row">'
        f'<div class="{depot_class}"></div>'
        '<div class="journey-copy"><div class="journey-kicker">Start</div>'
        f'<div class="journey-title">{depot_title}</div>'
        f'<div class="journey-meta">{depot_meta}</div></div></div>'
    )
    for index, _site in enumerate(state.job_sites, 1):
        if manifest_hint is not None and index <= len(manifest_hint.jobs):
            job = manifest_hint.jobs[index - 1]
            label = escape(site_and_task(job.name)[0])
            duration = job.duration_minutes
        else:
            label = f"Work site {index}"
            duration = DEFAULT_JOB_DURATION_MINUTES
        rows.append(
            '<div class="journey-row">'
            f'<div class="journey-node">{index}</div>'
            '<div class="journey-copy">'
            f'<div class="journey-kicker">Stop {index}</div>'
            f'<div class="journey-title">{label}</div>'
            f'<div class="journey-meta">{duration} min on site</div></div></div>'
        )
    if not state.job_sites:
        rows.append(
            '<div class="journey-row">'
            '<div class="journey-node pending">+</div>'
            '<div class="journey-copy"><div class="journey-kicker">Work stops</div>'
            '<div class="journey-title">Tap the map to add jobs</div>'
            '<div class="journey-meta">Add 2–9 places; pan as far as needed</div>'
            "</div></div>"
        )
    rows.append(
        '<div class="journey-row">'
        '<div class="journey-node return"></div>'
        '<div class="journey-copy"><div class="journey-kicker">Finish</div>'
        '<div class="journey-title">Return to crew base</div>'
        '<div class="journey-meta">The route ends where it started</div></div></div>'
    )
    st.markdown(
        '<div class="journey-panel"><div class="journey-eyebrow">Your workday</div>'
        '<div class="journey-list">' + "".join(rows) + "</div></div>",
        unsafe_allow_html=True,
    )


def _load_example_scenario(state: MapScenarioState) -> None:
    validation = validate_job_manifest(load_sample_jobs())
    if not validation.is_valid or validation.manifest is None:
        st.error("The bundled Phoenix walkthrough failed validation.")
        return
    manifest = validation.manifest
    example_state = MapScenarioState(
        operating_area_id=DEFAULT_OPERATING_AREA_ID,
        depot=MapPoint(EXAMPLE_DEPOT.latitude, EXAMPLE_DEPOT.longitude),
        job_sites=tuple(
            MapPoint(job.location.latitude, job.location.longitude)
            for job in manifest.jobs
        ),
    )
    save_map_scenario(example_state, source="example", source_manifest=manifest)
    st.session_state[MAP_GENERATION_KEY] = (
        int(st.session_state.get(MAP_GENERATION_KEY, 0)) + 1
    )
    st.rerun()


def _load_uploaded_scenario(state: MapScenarioState, manifest: JobManifest) -> None:
    imported_state = MapScenarioState(
        operating_area_id=state.operating_area_id,
        job_sites=tuple(
            MapPoint(job.location.latitude, job.location.longitude)
            for job in manifest.jobs
        ),
    )
    save_map_scenario(imported_state, source="upload", source_manifest=manifest)
    st.session_state[MAP_GENERATION_KEY] = (
        int(st.session_state.get(MAP_GENERATION_KEY, 0)) + 1
    )
    st.rerun()


def render_advanced_sources(state: MapScenarioState) -> None:
    """Keep spreadsheet import and the judge walkthrough out of onboarding."""

    with st.expander("Advanced: import work orders or load the walkthrough"):
        st.markdown("**Already export jobs from another system?**")
        st.caption(
            "Import is optional. The map above is the fastest way to start. "
            "Uploaded work orders stay in this app session."
        )
        upload_column, template_column = st.columns([1.6, 1])
        with upload_column:
            uploaded = st.file_uploader(
                "Import work orders (CSV)",
                type=["csv"],
                key="job_manifest_upload",
                help=(
                    f"UTF-8 CSV, 1 MB maximum, with {MIN_MANIFEST_JOBS}–"
                    f"{MAX_MANIFEST_JOBS} U.S. work sites."
                ),
            )
        with template_column:
            st.download_button(
                "Download import template",
                data=JOB_TEMPLATE_CSV,
                file_name="certiroute-work-orders-template.csv",
                mime="text/csv",
                width="stretch",
            )

        if uploaded is not None:
            validation = parse_uploaded_jobs(uploaded.getvalue())
            if not validation.is_valid or validation.manifest is None:
                st.error("This import needs a few fixes.")
                for message in validation.error_messages:
                    st.markdown(f"- {escape(message)}")
            else:
                imported = validation.manifest
                st.success(f"{len(imported.jobs)} work sites are ready to place.")
                preview = imported.frame[["name", "duration_minutes"]].rename(
                    columns={"name": "Work site", "duration_minutes": "Minutes"}
                )
                st.dataframe(preview, hide_index=True, width="stretch")
                if st.button(
                    "Use these imported jobs",
                    width="stretch",
                    key="use_imported_jobs",
                ):
                    _load_uploaded_scenario(state, imported)

        st.divider()
        st.markdown("**Want to see a finished setup first?**")
        st.caption(
            "Load six fictional Phoenix jobs at real landmarks. Temperature "
            "evidence still comes only from FortyGuard."
        )
        if st.button(
            "Load the Phoenix walkthrough",
            width="stretch",
            key="load_phoenix_walkthrough",
        ):
            _load_example_scenario(state)


def render_map_setup() -> MapScenarioState:
    """Render the map-first job input and apply at most one new click."""

    state = load_map_scenario()
    generation = int(st.session_state.get(MAP_GENERATION_KEY, 0))
    labels = [area.label for area in OPERATING_AREA_PRESETS]
    ids_by_label = {area.label: area.area_id for area in OPERATING_AREA_PRESETS}
    selected_label = st.selectbox(
        "Start near a U.S. city — pan anywhere in the U.S.",
        options=labels,
        index=labels.index(state.operating_area.label),
        key=f"operating_area_{generation}",
        help=(
            "This is only a shortcut for positioning the map. It does not set a "
            "route boundary."
        ),
    )
    selected_area_id = ids_by_label[selected_label]
    if selected_area_id != state.operating_area_id:
        changed = select_operating_area(state, selected_area_id)
        save_map_scenario(changed, source="map")
        st.session_state[MAP_GENERATION_KEY] = generation + 1
        st.rerun()

    manifest_hint = source_manifest()
    render_picker_instruction(state)
    map_column, journey_column = st.columns([1.6, 1], gap="large")
    with map_column:
        returned = map_picker.render_map_picker(
            state.operating_area,
            depot=state.depot,
            job_sites=state.job_sites,
            coverage=coverage_for(state.operating_area_id),
            generation=generation,
            height=470,
        )
    with journey_column:
        render_selection_summary(state, manifest_hint)
        undo_column, reset_column = st.columns(2)
        with undo_column:
            if st.button(
                "Undo last point",
                width="stretch",
                disabled=state.depot is None and not state.job_sites,
            ):
                save_map_scenario(undo_last_point(state), source="map")
                st.rerun()
        with reset_column:
            if st.button(
                "Start over",
                width="stretch",
                disabled=state.depot is None and not state.job_sites,
            ):
                start_fresh_map(state)
                st.rerun()

    last_clicked = (
        returned.get("last_clicked") if isinstance(returned, Mapping) else None
    )
    if isinstance(last_clicked, Mapping):
        latitude = last_clicked.get("lat")
        longitude = last_clicked.get("lng")
        try:
            result = apply_map_click(
                state,
                latitude=latitude,  # type: ignore[arg-type]
                longitude=longitude,  # type: ignore[arg-type]
            )
        except ValueError:
            st.warning("That map click could not be used. Please try a nearby point.")
        else:
            if result.action is MapClickAction.JOB_LIMIT_REACHED:
                save_map_scenario(
                    result.state,
                    source=st.session_state.get(SCENARIO_SOURCE_KEY, "map"),
                    source_manifest=manifest_hint,
                )
                st.warning(f"A route can include up to {MAX_MANIFEST_JOBS} work sites.")
            elif result.point_was_added:
                source = str(st.session_state.get(SCENARIO_SOURCE_KEY, "map"))
                keep_manifest = (
                    manifest_hint
                    if source == "upload" and result.action is MapClickAction.DEPOT_SET
                    else None
                )
                save_map_scenario(
                    result.state,
                    source=source if keep_manifest is not None else "map",
                    source_manifest=keep_manifest,
                )
                st.rerun()

    render_advanced_sources(state)
    return state


def render_workday_settings(
    *,
    scenario_token: str,
    is_example: bool,
    manifest_hint: JobManifest | None,
) -> tuple[date, time, time]:
    """Use useful defaults while keeping the historical mode explicit."""

    today = date.today()
    default_date = today
    if manifest_hint is None:
        default_start = DEFAULT_SHIFT_START
        default_end = DEFAULT_SHIFT_END
    else:
        default_start = time.fromisoformat(
            str(manifest_hint.frame["earliest_start"].min())
        )
        default_end = time.fromisoformat(
            str(manifest_hint.frame["latest_finish"].max())
        )

    with st.expander("Change the day or shift (optional)"):
        st.caption(
            "Today is planned from this morning's measured heat. Pick an earlier "
            "date to review a finished day against what actually happened."
        )
        selected_date = st.date_input(
            "Workday",
            value=default_date,
            min_value=date(2021, 1, 1),
            max_value=today,
            key=f"workday_date_{scenario_token}",
        )
        start_column, end_column = st.columns(2)
        with start_column:
            shift_start = st.time_input(
                "Crew normally starts",
                value=default_start,
                step=timedelta(minutes=15),
                help=(
                    "The shift you would run without heat planning. CertiRoute "
                    "compares its recommendation against this."
                ),
                key=f"shift_start_{scenario_token}",
            )
        with end_column:
            shift_end = st.time_input(
                "Crew finishes",
                value=default_end,
                step=timedelta(minutes=15),
                key=f"shift_end_{scenario_token}",
            )

    is_today = selected_date >= today
    mode_label = "Planning today" if is_today else "Reviewing a finished day"
    st.markdown(
        f'<div class="workday-chip"><span><strong>{mode_label}</strong></span>'
        f"<span>{selected_date.strftime('%d %b %Y')} · usual shift "
        f"{shift_start.strftime('%H:%M')}–{shift_end.strftime('%H:%M')}</span></div>",
        unsafe_allow_html=True,
    )
    return selected_date, shift_start, shift_end


def build_map_manifest(
    state: MapScenarioState,
    *,
    shift_start: time,
    shift_end: time,
    scenario_token: str,
) -> JobManifestValidation:
    """Create ordinary jobs from map points, with only useful edits exposed."""

    validation = build_default_job_manifest(
        state,
        shift_start=shift_start,
        shift_end=shift_end,
    )
    if not validation.is_valid or validation.manifest is None:
        return validation

    frame = validation.manifest.frame.copy()
    with st.expander("Name jobs or change visit time (optional)"):
        st.caption(
            "Every map point already works as a 45-minute job. Add names only if "
            "they will help the crew."
        )
        for index in range(len(frame)):
            name_column, duration_column = st.columns([2.2, 1])
            with name_column:
                frame.loc[index, "name"] = st.text_input(
                    f"Job {index + 1} name",
                    value=str(frame.loc[index, "name"]),
                    key=f"job_name_{scenario_token}_{index}",
                )
            with duration_column:
                frame.loc[index, "duration_minutes"] = st.number_input(
                    f"Minutes at job {index + 1}",
                    min_value=5,
                    max_value=240,
                    value=int(frame.loc[index, "duration_minutes"]),
                    step=5,
                    key=f"job_duration_{scenario_token}_{index}",
                )
    return validate_job_manifest(frame)


def scroll_to(anchor_id: str) -> None:
    """Bring a freshly rendered section into view.

    The plan appears near the top of the page while the button that produces
    it sits far below, so without this a dispatcher presses Plan and watches
    nothing visibly happen. Called only on the run that produced a new plan,
    never on ordinary reruns, so it cannot fight the user's own scrolling.
    """

    st.iframe(
        "<script>"
        "const doc = window.parent.document;"
        f'const target = doc.getElementById("{anchor_id}");'
        "if (target) {"
        'target.scrollIntoView({behavior: "smooth", block: "start"});'
        "}"
        "</script>",
        height=1,
    )


def coverage_for(area_id: str) -> map_picker.CoverageArea | None:
    """The circle a trained model may be applied inside, if one exists.

    Showing the boundary is the honest alternative to letting a dispatcher
    place work outside it and only then be refused after the effort of
    setting up a route.
    """

    model = trained_model(area_id)
    if model is None or model.trained_area is None:
        return None
    latitude, longitude = model.trained_area.centre
    return map_picker.CoverageArea(
        centre=MapPoint(latitude, longitude),
        radius_km=DEFAULT_TRAINED_RADIUS_KM,
        label=model.label,
    )


def trained_model(area_id: str) -> DiurnalClimatology | None:
    """Load the committed heat model for an area, or None when untrained."""

    try:
        return load_climatology(area_id, root=climatology_root())
    except (ClimatologyUnavailableError, ValueError):
        return None


PENDING_LEVEL_KEY = "certiroute_pending_level_activity"


def read_today_level(
    jobs: list[Job],
    store: HeatmapSnapshotStore,
    *,
    target_date: date,
    granularity: int,
) -> DailyLevelReading:
    """Fetch the one whole-day aggregate that anchors today's prediction.

    A reading that outran its polling budget is resumed rather than resubmitted,
    so pressing the button again rejoins the task already running instead of
    spending a second credit and abandoning the first.
    """

    pending = st.session_state.get(PENDING_LEVEL_KEY)
    resume = (
        pending.get("activity_ids", {})
        if isinstance(pending, dict) and pending.get("date") == target_date.isoformat()
        else {}
    )
    settings = get_settings()
    with FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    ) as client:
        reading = collect_clustered_daily_level(
            jobs,
            store,
            target_date=target_date,
            granularity=granularity,
            client=client,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
            resume_activity_ids=resume,
        )
        st.session_state.pop(PENDING_LEVEL_KEY, None)
        return reading


def candidate_starts_for(shift_start: time, shift_end: time) -> tuple[time, ...]:
    """Offer earlier starts than the crew's usual one, plus that one itself.

    Only whole hours the trained model can actually speak to are offered, and
    the shift keeps its length so the comparison is like for like.
    """

    latest = minutes_of_day(shift_start)
    duration = minutes_of_day(shift_end) - latest
    earliest = max(5 * 60, latest - 3 * 60)
    starts = [
        minute
        for minute in range(earliest, latest + 1, 60)
        if minute + duration <= minutes_of_day(shift_end)
        or minute == latest
    ]
    if latest not in starts:
        starts.append(latest)
    return tuple(time(minute // 60, minute % 60) for minute in sorted(set(starts)))


def render_start_decision(plan: SameDayPlan) -> None:
    """State the one decision this product exists to make."""

    start = plan.recommended_start.strftime("%H:%M")
    baseline = plan.baseline_start.strftime("%H:%M")
    reduction = plan.exposure_reduction
    first_site = escape(site_and_task(plan.crew_plan.stops[0].job_name)[0])

    if plan.changes_the_start and reduction is not None and reduction >= 0.005:
        hours, minutes = divmod(abs(plan.minutes_earlier), 60)
        if hours and minutes:
            amount = f"{hours}h {minutes}m"
        elif hours:
            amount = "an hour" if hours == 1 else f"{hours} hours"
        else:
            amount = f"{minutes} minutes"
        direction = "earlier" if plan.minutes_earlier > 0 else "later"
        label = "Move the shift"
        title = f"Start at {start}"
        explanation = (
            f"Beginning {amount} {direction} than your usual {baseline} cuts "
            f"modelled heat exposure by <strong>{reduction:.0%}</strong> for the "
            f"same work. First stop is <strong>{first_site}</strong>."
        )
    else:
        label = "Keep the shift"
        title = f"Start at {start} as usual"
        explanation = (
            "No earlier start meaningfully reduces exposure on today's predicted "
            f"heat, so there is no reason to move the crew. First stop is "
            f"<strong>{first_site}</strong>."
        )

    finish = minute_label(plan.crew_plan.route_finish_minute)
    peak = max(
        stop.temperature_c for stop in plan.crew_plan.stops
    )
    st.markdown(
        f"""
        <div class="bento">
          <div class="bento-hero decision-card">
            <div class="decision-label">{icon("sunrise", size=13)}{label}</div>
            <h2>{title}</h2>
            <p>{explanation}</p>
          </div>
          <div class="bento-tile route-fact time">
            <div class="route-fact-label">{icon("clock", size=13)}Shift window</div>
            <div class="route-fact-value">{start}&nbsp;&ndash;&nbsp;{finish}</div>
          </div>
          <div class="bento-tile route-fact stop">
            <div class="route-fact-label">
              {icon("thermometer", size=13)}Hottest working moment
            </div>
            <div class="route-fact-value">{peak:.1f} &deg;C</div>
          </div>
          <div class="bento-tile route-fact status">
            <div class="route-fact-label">
              {icon("gauge", size=13)}Predicted within
            </div>
            <div class="route-fact-value">
              &plusmn;{plan.interval_radius_c:.1f} &deg;C at {plan.coverage:.0%}
            </div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_day_playback(plan: SameDayPlan) -> None:
    """Let the decision be watched instead of read.

    Two crews leave the same base on the same route; one is home before the
    heat arrives and the other is still out in it. That is the entire argument
    for moving a shift, and it lands in seconds where a percentage does not.
    """

    by_start = {
        option.shift_start: option.plan
        for option in plan.comparison.options
        if option.feasible and option.plan is not None
    }
    recommended = by_start.get(plan.recommended_start)
    usual = by_start.get(plan.baseline_start)
    if recommended is None or not recommended.stops:
        return

    runs = [
        PlaybackRun(
            label=f"Recommended {plan.recommended_start:%H:%M}",
            plan=recommended,
            color=ROUTE_COLOR,
            recommended=True,
        )
    ]
    if usual is not None and usual.stops and plan.changes_the_start:
        runs.append(
            PlaybackRun(
                label=f"Your usual {plan.baseline_start:%H:%M}",
                plan=usual,
                color=HEAT_COLOR,
            )
        )

    st.markdown("### Watch the day")
    st.caption(
        "The same jobs in the same order, run from two different start times. "
        "Temperatures are the ones this plan was built against."
    )
    try:
        markup = route_playback_html(
            runs,
            plan.conservative_profiles,
            depot=plan.depot,
            threshold_c=PLANNING_THRESHOLD_C,
        )
    except ValueError:
        return
    st.iframe(markup, height=620 if len(runs) > 1 else 560)


def render_start_options(plan: SameDayPlan) -> None:
    """Show every start time considered, so the choice is legible."""

    feasible = [
        option
        for option in plan.comparison.options
        if option.feasible and option.exposure_units is not None
    ]
    if len(feasible) < 2:
        return
    by_start = {option.shift_start: option.exposure_units for option in feasible}
    usual = by_start.get(plan.baseline_start)
    worst = max(by_start.values())
    if not usual or worst <= 0:
        return

    # Exposure never approaches zero across these options, so a zero-based bar
    # would make every start look alike. The bar shows heat avoided against the
    # crew's usual start, which is the quantity the decision actually turns on,
    # and the usual start is drawn as the empty baseline it is.
    savings = {start: max(usual - units, 0.0) for start, units in by_start.items()}
    best_saving = max(savings.values())

    rows: list[str] = []
    for option in feasible:
        saving = savings[option.shift_start]
        share = 0.0 if best_saving <= 0 else saving / best_saving
        is_pick = option.shift_start == plan.recommended_start
        is_usual = option.shift_start == plan.baseline_start
        if is_usual:
            value = "your usual"
        elif saving <= 0:
            value = "no gain"
        else:
            value = f"{saving / usual:.0%} cooler"
        rows.append(
            f'<div class="timing-row{" picked" if is_pick else ""}">'
            f'<div class="timing-label">{option.shift_start.strftime("%H:%M")}</div>'
            '<div class="timing-track">'
            f'<div class="timing-bar" style="width:{max(share, 0.015):.1%}"></div>'
            "</div>"
            f'<div class="timing-tag">{escape(value)}</div>'
            "</div>"
        )
    st.markdown(
        "### Why this start\n"
        "Same jobs, same shift length, different starting hour. Longer bars mean "
        "more heat avoided against your usual start."
    )
    st.markdown(
        '<div class="timing-bars">' + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )
    held = plan.windows.held_job_ids
    if held:
        blocking = plan.windows.earliest_held_start
        st.caption(
            f"{len(held)} site(s) cannot be visited before "
            f"{blocking.strftime('%H:%M') if blocking else 'their access window'} "
            "because of their own access windows, so those hours were not "
            "available to move: " + ", ".join(escape(job_id) for job_id in held) + "."
        )


def render_model_provenance(plan: SameDayPlan) -> None:
    """Say exactly what evidence stands behind the prediction."""

    model = plan.climatology
    evaluation = model.evaluation
    reading = plan.level_reading
    unseen = (
        "not measured"
        if evaluation.unseen_site_mae_c is None
        else f"{evaluation.unseen_site_mae_c:.2f} °C"
    )
    st.markdown("#### What this prediction is built on")
    columns = st.columns(4)
    columns[0].metric(
        "Today's measured level",
        f"{reading.area_mean_c:.1f} °C",
        help=(
            "FortyGuard's whole-day aggregate for this area, read "
            f"{reading.collected_at_utc:%d %b %H:%M} UTC. This is the only "
            "same-day signal the API returns; hourly data is historical only."
        ),
    )
    columns[1].metric(
        "Held-out error",
        f"{evaluation.mean_absolute_error_c:.2f} °C",
        help=(
            "Mean absolute error on "
            f"{len(evaluation.holdout_dates)} day(s) the model never trained on. "
            f"Worst single reading: {evaluation.worst_absolute_error_c:.2f} °C."
        ),
    )
    columns[2].metric(
        "Error at unseen sites",
        unseen,
        help=(
            "Each site was removed from training in turn and predicted from the "
            "others. This is what justifies applying one area model to work "
            "sites you place yourself."
        ),
    )
    columns[3].metric(
        "Trained on",
        f"{len(model.training_dates)} days",
        help=(
            "Hour offsets learned from "
            f"{model.training_dates[0]:%d %b} to {model.training_dates[-1]:%d %b} "
            f"at {model.granularity_m} m resolution."
        ),
    )
    st.caption(
        f"Interval: ± {plan.interval_radius_c:.1f} °C at {plan.coverage:.0%} "
        "coverage, from a split-conformal quantile over "
        f"{len(evaluation.day_scores_c)} held-out day(s). Whole days are the "
        "unit, not individual readings, because a day runs hot or cool as a "
        "whole. Fewer days means a wider honest interval, never a tighter "
        "claim. The plan is built on the top of this interval, not its middle."
    )


def render_ordering_finding(plan: SameDayPlan) -> None:
    """Report the reordering result honestly, including when it is nothing."""

    st.markdown("#### Does the visit order matter today?")
    reduction = relative_exposure_reduction(
        plan.efficient_plan.total_raw_exposure_units,
        plan.heat_aware_plan.total_raw_exposure_units,
    )
    if plan.reorder_changes_sequence and reduction and reduction >= 0.005:
        st.success(
            f"Yes — reordering the stops avoids a further {reduction:.1%} of "
            "modelled exposure at this start time."
        )
    else:
        st.info(
            "No. At this start time the heat-aware search found no sequence "
            "worth changing. That matches what backtesting showed across "
            "Phoenix, Houston and Miami: site-to-site spread (0.32–2.32 °C) is "
            "far smaller than the swing across the day (5.2–9.3 °C), so *when* "
            "the crew works dominates *what order* they work in. CertiRoute "
            "keeps the efficient order and moves the shift instead."
        )


def render_predicted_conditions(plan: SameDayPlan) -> None:
    """Expose the exact predicted numbers behind the schedule."""

    st.markdown("#### Predicted conditions at each stop")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Stop": stop.sequence,
                    "Work order": stop.job_id,
                    "Site and task": stop.job_name,
                    "Start": minute_label(stop.start_minute),
                    "Finish": minute_label(stop.finish_minute),
                    "Planned-against temperature": f"{stop.temperature_c:.1f} °C",
                }
                for stop in plan.crew_plan.stops
            ]
        ),
        hide_index=True,
        width="stretch",
        column_config={
            "Stop": st.column_config.NumberColumn(width="small"),
            "Work order": st.column_config.TextColumn(width="small"),
            "Site and task": st.column_config.TextColumn(width="large"),
        },
    )
    st.caption(
        "These are upper-bound temperatures: the prediction plus the calibrated "
        f"± {plan.interval_radius_c:.1f} °C. A day that runs hotter than "
        "expected still lands inside the assumption this schedule was built on."
    )


def render_hindsight(
    jobs: list[Job],
    batch: RealTemperatureBatch,
    model: DiurnalClimatology | None,
    *,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
    target_date: date,
) -> None:
    """Score the recommendation this model would have made, against reality.

    This is the check that makes the rest of the product worth trusting: the
    model predicts the day from a single aggregate, picks a start, and is then
    graded on the temperatures that were actually measured.
    """

    st.markdown("### Did the recommendation hold up?")
    if model is None:
        st.info(
            "No trained heat model covers this area, so there is no "
            "recommendation to grade against this day."
        )
        return
    seen = set(model.training_dates) | set(model.evaluation.holdout_dates)
    if target_date in seen:
        st.info(
            f"{target_date:%d %b %Y} is one of the days this model was built "
            "from, so grading it would measure memory rather than skill. Pick a "
            "date outside "
            f"{min(seen):%d %b}–{max(seen):%d %b} to see an honest check."
        )
        return

    candidates = candidate_starts_for(shift_start, shift_end)
    if not st.button("Grade this day", width="stretch"):
        st.caption(
            "One FortyGuard request: this day's whole-day reading. The hourly "
            "temperatures above are already collected."
        )
        return

    try:
        with st.spinner("Rebuilding the morning's recommendation…"):
            reading = read_today_level(
                jobs,
                HeatmapSnapshotStore(cache_path()),
                target_date=target_date,
                granularity=model.granularity_m,
            )
            plan = build_same_day_plan(
                jobs,
                model,
                reading,
                depot=depot,
                baseline_start=shift_start,
                candidate_starts=candidates,
                shift_end=shift_end,
                average_travel_speed_kph=AVERAGE_TRAVEL_SPEED_KPH,
                reference_temperature_c=REFERENCE_TEMPERATURE_C,
                planning_threshold_c=PLANNING_THRESHOLD_C,
                uncertainty_penalty=0.0,
                heat_weight=HEAT_WEIGHT,
            )
            outcome = score_plan_against_measurements(
                plan,
                batch.profiles,
                depot=depot,
                candidate_starts=candidates,
                shift_end=shift_end,
                average_travel_speed_kph=AVERAGE_TRAVEL_SPEED_KPH,
                reference_temperature_c=REFERENCE_TEMPERATURE_C,
                planning_threshold_c=PLANNING_THRESHOLD_C,
                uncertainty_penalty=0.0,
                heat_weight=HEAT_WEIGHT,
            )
    except (
        LeakageError,
        OutsideTrainedAreaError,
        PlanningCoverageError,
        ProfileCoverageError,
    ) as exc:
        st.info(f"This day cannot be graded: {exc}")
        return
    except (
        CacheCorruptionError,
        FortyGuardError,
        HeatmapCoverageError,
        InfeasibleScheduleError,
        InsufficientHistoryError,
        LookupError,
        ScheduleSearchLimitError,
        ValidationError,
        ValueError,
    ) as exc:
        st.error(f"This day could not be graded: {exc}")
        return

    realized = outcome.realized_reduction
    columns = st.columns(4)
    columns[0].metric(
        "It recommended",
        outcome.recommended_start.strftime("%H:%M"),
        help="Chosen from the whole-day reading alone, before seeing any hour.",
    )
    columns[1].metric(
        "Best in hindsight",
        outcome.realized_best_start.strftime("%H:%M"),
        help="The lowest-exposure start once every measured temperature is known.",
    )
    columns[2].metric(
        "Exposure actually avoided",
        "n/a" if realized is None else f"{realized:.1%}",
        help=(
            "Measured against your usual "
            f"{outcome.baseline_start.strftime('%H:%M')} start, on the "
            "temperatures this day really produced."
        ),
    )
    columns[3].metric(
        "Regret",
        f"{outcome.regret_units:.1f}",
        help=(
            "Degree-hours above the perfect-hindsight start. Zero means the "
            "recommendation was optimal."
        ),
        delta_color="inverse",
    )

    if outcome.chose_the_best_start:
        st.success(
            f"On {target_date:%d %b %Y} the model picked the best available "
            "start without seeing a single hourly temperature."
        )
    elif outcome.helped:
        st.warning(
            "The recommendation reduced exposure against the usual start, but "
            f"{outcome.realized_best_start.strftime('%H:%M')} would have been "
            f"better by {outcome.regret_units:.1f} degree-hours."
        )
    else:
        st.error(
            "On this day the recommendation did not beat the usual start. This "
            "is reported rather than hidden; it is what an honest check is for."
        )


def render_same_day_result(plan: SameDayPlan, depot: GeoPoint) -> None:
    """Crew view first; the model's workings stay one click away."""

    st.markdown(
        '<div id="certiroute-plan"></div>', unsafe_allow_html=True
    )
    section(3, "Today's plan")
    view = st.segmented_control(
        "Result view",
        options=["Crew route", "Planner details"],
        default="Crew route",
        key="view_mode",
        label_visibility="collapsed",
    )
    if view == "Planner details":
        st.markdown("### Planner details")
        st.caption(
            "For dispatchers and reviewers: what the prediction rests on, how "
            "wide it is, and what the scheduler did with it."
        )
        render_model_provenance(plan)
        render_ordering_finding(plan)
        render_predicted_conditions(plan)
        render_safety_boundary(compact=False)
        return

    render_start_decision(plan)
    render_day_playback(plan)
    render_start_options(plan)
    render_run_sheet(plan.crew_plan, depot)
    render_safety_boundary(compact=True)


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
    plan: ClusteredHeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
) -> RealTemperatureBatch:
    """Collect the exact missing set, or rebuild profiles entirely from cache."""

    if plan.new_task_count == 0:
        return collect_clustered_real_temperature_batch_from_plan(
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
        return collect_clustered_real_temperature_batch_from_plan(
            jobs,
            plan,
            store,
            client=client,
            poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
            max_attempts=settings.fortyguard_max_poll_attempts,
            max_new_tasks=plan.new_task_count,
            now_utc=datetime.now(UTC),
        )


st.set_page_config(
    page_title="CertiRoute",
    page_icon=str(PROJECT_ROOT / "app" / "assets" / "favicon.png"),
    layout="wide",
)
inject_styles()
render_hero()
render_landing_proof()
render_three_steps(
    load_map_scenario(),
    planned=isinstance(
        st.session_state.get("certiroute_today_plan"), SameDayPlan
    ),
)
route_result_slot = st.container()

section(
    1,
    "Build a route from the map",
    blurb="Tap the crew base, then every place the crew has to be.",
)
map_state = render_map_setup()
if not map_state.is_ready or map_state.depot is None:
    st.markdown("---")
    render_safety_boundary(compact=True)
    st.stop()

source_token = str(st.session_state.get(SCENARIO_SOURCE_KEY, "map"))
manifest_hint = source_manifest()
if manifest_hint is not None and len(manifest_hint.jobs) != map_state.job_count:
    manifest_hint = None
    source_token = "map"
generation = int(st.session_state.get(MAP_GENERATION_KEY, 0))
widget_token = f"{source_token}_{generation}"
is_example = source_token == "example"
selected_date, shift_start, shift_end = render_workday_settings(
    scenario_token=widget_token,
    is_example=is_example,
    manifest_hint=manifest_hint,
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

if manifest_hint is None:
    validation = build_map_manifest(
        map_state,
        shift_start=shift_start,
        shift_end=shift_end,
        scenario_token=widget_token,
    )
    if not validation.is_valid or validation.manifest is None:
        st.error("One or more job details need attention before routing.")
        for message in validation.error_messages:
            st.markdown(f"- {escape(message)}")
        render_safety_boundary(compact=True)
        st.stop()
    manifest = validation.manifest
else:
    manifest = manifest_hint

domain_jobs = list(manifest.jobs)
depot = GeoPoint(
    latitude=map_state.depot.latitude,
    longitude=map_state.depot.longitude,
)
planning_today = selected_date >= date.today()
area_model = trained_model(map_state.operating_area_id)

if planning_today:
    section(
        2,
        "Plan today's shift",
        blurb="One reading of today's heat decides when the crew starts.",
    )
    if area_model is None:
        st.warning(
            f"CertiRoute has no trained heat model for "
            f"**{escape(map_state.operating_area.label)}** yet, so it cannot "
            "predict today there. Trained areas can be planned live; anywhere "
            "else, pick a past date above to review a finished day against "
            "measured temperatures."
        )
        render_safety_boundary(compact=True)
        st.stop()

    candidate_starts = candidate_starts_for(shift_start, shift_end)
    st.markdown(
        f"""
        <div class="build-summary">
          <strong>{len(domain_jobs)} work sites · {escape(area_model.label)}</strong>
          CertiRoute reads today's measured heat, predicts the hours ahead, and
          chooses the coolest start between
          {candidate_starts[0].strftime("%H:%M")} and
          {shift_start.strftime("%H:%M")} that still fits every job.
        </div>
        """,
        unsafe_allow_html=True,
    )
    key_available = api_key_available()
    plan_clicked = st.button(
        "Plan today's shift",
        type="primary",
        width="stretch",
        disabled=not key_available,
    )
    st.caption(
        "One FortyGuard request is sent when you press this: today's whole-day "
        "reading for this area. The hour-by-hour shape is already trained and "
        "committed. Nothing is invented when data is missing."
    )
    if not key_available:
        st.error(
            "FortyGuard connection required. Add `FORTYGUARD_API_KEY` to `.env` "
            "and reload. No substitute temperature data will be generated."
        )

    today_key = (
        map_state.operating_area_id,
        manifest.fingerprint,
        depot.latitude,
        depot.longitude,
        shift_start.isoformat(),
        shift_end.isoformat(),
        selected_date.isoformat(),
    )
    if st.session_state.get("certiroute_today_key") != today_key:
        st.session_state.pop("certiroute_today_plan", None)
        st.session_state.pop("view_mode", None)
    same_day_plan = st.session_state.get("certiroute_today_plan")
    if not isinstance(same_day_plan, SameDayPlan):
        same_day_plan = None

    if plan_clicked:
        try:
            with st.status("Reading today's heat…", expanded=True) as status:
                step_done(f"Read {len(domain_jobs)} work sites and the crew base")
                preflight_route(
                    domain_jobs,
                    depot=depot,
                    shift_start=shift_start,
                    shift_end=shift_end,
                )
                step_done("Confirmed the crew can finish every job in this shift")
                reading = read_today_level(
                    domain_jobs,
                    HeatmapSnapshotStore(cache_path()),
                    target_date=selected_date,
                    granularity=area_model.granularity_m,
                )
                step_done(
                    f"Today measures {reading.area_mean_c:.1f} °C "
                    "across this area"
                )
                same_day_plan = build_same_day_plan(
                    domain_jobs,
                    area_model,
                    reading,
                    depot=depot,
                    baseline_start=shift_start,
                    candidate_starts=candidate_starts,
                    shift_end=shift_end,
                    average_travel_speed_kph=AVERAGE_TRAVEL_SPEED_KPH,
                    reference_temperature_c=REFERENCE_TEMPERATURE_C,
                    planning_threshold_c=PLANNING_THRESHOLD_C,
                    uncertainty_penalty=0.0,
                    heat_weight=HEAT_WEIGHT,
                )
                step_done("Compared every start time the crew could work")
                status.update(label="Today's plan is ready", state="complete")
        except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
            same_day_plan = None
            st.error(
                "No complete depot-to-depot route fits these job windows and "
                "shift. Shorten a visit, lengthen the shift, or remove a distant "
                f"job, then try again. Details: {exc}"
            )
        except DailyLevelPendingError as exc:
            same_day_plan = None
            # The task is still running on FortyGuard's side, so remember it and
            # rejoin rather than paying for a second one.
            pending = st.session_state.get(PENDING_LEVEL_KEY)
            known = (
                dict(pending.get("activity_ids", {}))
                if isinstance(pending, dict)
                and pending.get("date") == selected_date.isoformat()
                else {}
            )
            known[len(known)] = exc.activity_id
            st.session_state[PENDING_LEVEL_KEY] = {
                "date": selected_date.isoformat(),
                "activity_ids": known,
            }
            st.warning(
                f"{exc} Large areas can take several minutes. Press "
                "**Plan today's shift** again to pick the same reading back up "
                "— it keeps computing in the background and no second "
                "request is sent."
            )
        except DailyLevelUnavailableError as exc:
            same_day_plan = None
            st.warning(
                f"FortyGuard has not published today's reading for this area "
                f"yet, so there is nothing measured to plan from. {exc} Try "
                "again later in the morning, or review a finished day below."
            )
        except OutsideTrainedAreaError as exc:
            same_day_plan = None
            st.error(
                "These work sites are outside the area this heat model was "
                f"trained on, so CertiRoute will not predict for them. {exc}"
            )
            st.info(
                "Move the map back to a trained operating area, or pick a past "
                "date to review a finished day from measured temperatures."
            )
        except (PlanningCoverageError, ProfileCoverageError) as exc:
            same_day_plan = None
            st.error(
                f"This shift reaches beyond what the trained model covers: {exc}"
            )
        except HeatmapCoverageError as exc:
            same_day_plan = None
            st.error(
                "FortyGuard returned no temperature tile covering one of these "
                f"sites. Move that point slightly and try again. Details: {exc}"
            )
        except InsufficientHistoryError as exc:
            same_day_plan = None
            st.error(f"The trained model cannot support this plan: {exc}")
        except (
            CacheCorruptionError,
            FortyGuardError,
            LookupError,
            ValidationError,
            ValueError,
        ) as exc:
            same_day_plan = None
            st.error(f"Today's heat reading could not be completed: {exc}")

    if same_day_plan is not None:
        st.session_state["certiroute_today_key"] = today_key
        st.session_state["certiroute_today_plan"] = same_day_plan
        with route_result_slot:
            render_result_mode_styles()
            render_same_day_result(same_day_plan, depot)
        if plan_clicked:
            scroll_to("certiroute-plan")
    else:
        render_empty_state(job_count=len(domain_jobs))
        st.markdown("---")
        render_safety_boundary(compact=True)
    st.stop()

# Reviewing a day exists to grade earlier starts against it, so the measured
# hours have to reach back to the earliest candidate. Collecting only the
# entered shift would leave the grader unable to see the hours it must judge.
review_candidates = candidate_starts_for(shift_start, shift_end)
sample_times = hourly_sample_times(min(review_candidates), shift_end)
try:
    profile_requests = build_clustered_profile_requests(
        domain_jobs,
        target_date=selected_date,
        sample_times=sample_times,
        granularity=GRANULARITY_METRES,
    )
    store = HeatmapSnapshotStore(cache_path())
    with st.spinner("Checking saved FortyGuard evidence…"):
        collection_plan = plan_clustered_profile_collection(
            profile_requests,
            store,
            now_utc=datetime.now(UTC),
        )
except CacheCorruptionError as exc:
    st.error(f"Saved FortyGuard evidence failed its integrity check: {exc}")
    st.stop()
except (ValidationError, ValueError) as exc:
    st.error(
        "These map points cannot be prepared for FortyGuard. Keep the crew's "
        f"locations within U.S. coverage and try again. Details: {exc}"
    )
    st.stop()

key_available = api_key_available()
section(
    2,
    "Create the crew route",
    blurb="Measured temperatures for every hour this shift could use.",
)
st.markdown(
    f"""
    <div class="build-summary">
      <strong>{len(domain_jobs)} work sites · start and finish at the mint base</strong>
      Real FortyGuard temperature intelligence for
      {selected_date.strftime("%d %b %Y")}, turned into one numbered visit order.
    </div>
    """,
    unsafe_allow_html=True,
)
build_clicked = st.button(
    "Create my heat-aware route",
    type="primary",
    width="stretch",
    disabled=(collection_plan.new_task_count > 0 and not key_available),
)
st.caption(
    "Nothing is sent until you press this button. Missing temperature evidence "
    "is never replaced with invented data."
)
if collection_plan.aoi_count > 1:
    st.caption(
        f"Your stops span {collection_plan.aoi_count} heat-data areas. "
        "CertiRoute will collect and combine them automatically."
    )

if collection_plan.new_task_count > 0 and not key_available:
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
    *(
        heatmap_request_fingerprint(request)
        for request in profile_requests.requests_by_key.values()
    ),
)
if st.session_state.get("certiroute_active_scenario") != scenario_key:
    st.session_state["certiroute_active_scenario"] = scenario_key
    st.session_state.pop("view_mode", None)

batch: RealTemperatureBatch | None = None
if st.session_state.get("certiroute_result_scenario") == scenario_key:
    saved_batch = st.session_state.get("certiroute_temperature_batch")
    if isinstance(saved_batch, RealTemperatureBatch):
        batch = saved_batch

if build_clicked:
    try:
        with st.status(
            "Checking heat and building the route…", expanded=True
        ) as status:
            step_done(f"Read {len(domain_jobs)} work sites and the crew base")
            preflight_route(
                domain_jobs,
                depot=depot,
                shift_start=shift_start,
                shift_end=shift_end,
            )
            step_done("Confirmed the crew can finish every job in this shift")
            progress = st.progress(
                collection_plan.cache_hit_count / collection_plan.request_count,
                text=("Checking real FortyGuard temperatures for every work hour"),
            )
            batch = collect_batch(domain_jobs, collection_plan, store)
            progress.progress(
                1.0,
                text="Real temperature evidence is ready",
            )
            step_done("Compared feasible visit orders and kept every job on time")
            status.update(label="Crew route ready", state="complete")
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        batch = None
        st.error(
            "No complete depot-to-depot route fits these job windows and shift. "
            "Shorten a visit, lengthen the shift, or remove a distant job, "
            "then try again. "
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
            model=area_model,
            target_date=selected_date,
        )
else:
    render_empty_state(
        job_count=len(domain_jobs),
    )
    st.markdown("---")
    render_safety_boundary(compact=True)
