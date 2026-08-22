"""Route-first Streamlit interface for the CertiRoute hackathon product.

The default view is a crew hand-off: one decision, one numbered map, and one
ordered stop list. Model comparison, temperatures, and API provenance live in
a separate planner view so they remain auditable without crowding the route.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from html import escape
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
from certiroute.optimization import (
    InfeasibleScheduleError,
    SchedulePlan,
    ScheduleSearchLimitError,
    ScheduleStrategy,
    compare_schedules,
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

DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)
DEFAULT_REPLAY_DATE = date(2026, 7, 15)
SHIFT_START = time(8)
SHIFT_END = time(17)
HOURLY_SAMPLE_TIMES = tuple(time(hour) for hour in range(8, 18))
GRANULARITY_METRES = 60
REFERENCE_TEMPERATURE_C = 27.0
PLANNING_THRESHOLD_C = 35.0
HEAT_WEIGHT = 8.0
PLAN_LABELS = {
    ScheduleStrategy.EFFICIENCY: "Distance-efficient operations baseline",
    ScheduleStrategy.HEAT_AWARE: "Heat-aware recommendation",
}


@st.cache_data
def load_sample_jobs() -> pd.DataFrame:
    """Load the committed, non-sensitive demonstration work orders."""

    return pd.read_csv(SAMPLE_JOBS_PATH)


def cache_path() -> Path:
    """Return an injectable cache root so tests and deployments stay isolated."""

    configured = os.getenv("CERTIROUTE_HEATMAP_CACHE_PATH")
    return Path(configured) if configured else DEFAULT_CACHE_PATH


def build_domain_jobs(frame: pd.DataFrame) -> list[Job]:
    """Convert input rows into validated scheduler jobs."""

    return [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
            earliest_start=time.fromisoformat(row.earliest_start),
            latest_finish=time.fromisoformat(row.latest_finish),
        )
        for row in frame.itertuples(index=False)
    ]


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
        .stApp { background: #f6f8f7; }
        .block-container { max-width: 1180px; padding-top: 1.6rem; }
        h1 { letter-spacing: -0.045em; }
        h2, h3 { letter-spacing: -0.025em; }
        .hero-copy {
            color: #334155; font-size: 1.08rem; line-height: 1.55;
            max-width: 780px; margin-top: -0.45rem;
        }
        .eyebrow {
            color: #087f5b; font-size: .77rem; font-weight: 800;
            letter-spacing: .12em; text-transform: uppercase;
        }
        .badge {
            display: inline-block; border: 1px solid #b8d8cd;
            background: #edf8f4; color: #116149; border-radius: 999px;
            padding: .28rem .62rem; margin: .15rem .3rem .15rem 0;
            font-size: .72rem; font-weight: 750; letter-spacing: .035em;
        }
        .process-strip {
            display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;
            color: #334155; margin: 1rem 0 1.25rem;
        }
        .process-step {
            display: inline-flex; align-items: center; gap: .45rem;
            border: 1px solid #d5e3de; background: white;
            border-radius: 999px; padding: .42rem .72rem; font-weight: 700;
        }
        .process-number {
            display: inline-flex; align-items: center; justify-content: center;
            width: 1.45rem; height: 1.45rem; border-radius: 50%;
            color: white; background: #087f5b; font-size: .78rem;
        }
        .process-arrow { color: #8aa199; font-weight: 800; }
        .empty-state {
            text-align: center; padding: 1.7rem 1rem; border: 1px dashed #9fb7af;
            border-radius: .75rem; background: #fbfdfc;
        }
        .safety-note {
            border-left: 4px solid #d97706; background: #fff9ed;
            padding: .85rem 1rem; border-radius: .35rem; color: #5f430b;
        }
        .decision-card {
            border: 1px solid #b9d8ce; border-left: 6px solid #087f5b;
            background: #f2fbf7; border-radius: .8rem; padding: 1.15rem 1.25rem;
            margin: .65rem 0 1rem;
        }
        .decision-card h2 { margin: .15rem 0 .35rem; color: #17352c; }
        .decision-card p { margin: 0; color: #39564d; line-height: 1.5; }
        .decision-label {
            color: #087f5b; font-size: .72rem; font-weight: 850;
            letter-spacing: .11em; text-transform: uppercase;
        }
        .route-summary {
            display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: .65rem; margin: .8rem 0 1.2rem;
        }
        .route-fact {
            background: white; border: 1px solid #dce5e1; border-radius: .7rem;
            padding: .78rem .9rem; min-width: 0;
        }
        .route-fact-label {
            color: #64748b; font-size: .7rem; font-weight: 800;
            letter-spacing: .08em; text-transform: uppercase;
        }
        .route-fact-value {
            color: #172b25; font-size: 1.08rem; font-weight: 800;
            margin-top: .12rem; overflow-wrap: anywhere;
        }
        .route-stop {
            display: grid; grid-template-columns: 2.65rem minmax(0, 1fr) auto;
            gap: .72rem; align-items: center; background: white;
            border: 1px solid #dce5e1; border-radius: .72rem;
            padding: .7rem .78rem; margin-bottom: .5rem; min-width: 0;
        }
        .route-stop-number {
            display: flex; align-items: center; justify-content: center;
            width: 2.35rem; height: 2.35rem; border-radius: 50%;
            color: white; background: #e8590c; font-weight: 850; font-size: 1rem;
        }
        .route-stop-copy { min-width: 0; }
        .route-stop-kicker {
            color: #087f5b; font-size: .66rem; font-weight: 850;
            letter-spacing: .08em; text-transform: uppercase;
        }
        .route-stop-name {
            color: #182c26; font-weight: 800; line-height: 1.25;
            overflow-wrap: anywhere;
        }
        .route-stop-task {
            color: #64748b; font-size: .78rem; line-height: 1.25;
            overflow-wrap: anywhere; margin-top: .12rem;
        }
        .route-stop-time {
            color: #253f36; font-weight: 800; white-space: nowrap;
            text-align: right;
        }
        .route-stop-travel {
            color: #64748b; font-size: .72rem; margin-top: .12rem;
        }
        .route-return {
            border: 1px dashed #9fb7af; border-radius: .65rem;
            color: #39564d; padding: .62rem .78rem; margin-top: .2rem;
            background: #fbfdfc; overflow-wrap: anywhere;
        }
        .map-note {
            color: #64748b; font-size: .78rem; line-height: 1.4;
            margin: .15rem 0 .55rem;
        }
        div[data-testid="stMetric"] {
            background: white; border: 1px solid #dce5e1;
            padding: .85rem 1rem; border-radius: .7rem;
        }
        button[kind="primary"] {
            background: #087f5b; border-color: #087f5b;
        }
        button[kind="primary"]:hover {
            background: #06694b; border-color: #06694b;
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
    st.subheader("A clear, numbered work route built around Phoenix heat.")
    st.markdown(
        """
        <div class="hero-copy">
        Choose a replay date. CertiRoute checks the same six jobs against real
        FortyGuard temperatures and returns one crew-ready stop order.
        </div>
        <div style="margin-top:.7rem">
          <span class="badge">REAL FORTYGUARD TEMPERATURES</span>
          <span class="badge">HISTORICAL REPLAY</span>
          <span class="badge">DEMO WORK ORDERS</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_three_steps() -> None:
    """Give first-time visitors a compact three-step mental model."""

    st.markdown(
        """
        <div class="process-strip" aria-label="How CertiRoute works">
          <span class="process-step">
            <span class="process-number">1</span>Choose date
          </span>
          <span class="process-arrow">→</span>
          <span class="process-step">
            <span class="process-number">2</span>Build route
          </span>
          <span class="process-arrow">→</span>
          <span class="process-step">
            <span class="process-number">3</span>Follow stops 1–6
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


def google_maps_route_url(plan: SchedulePlan) -> str:
    """Build a Google Maps directions URL that preserves the planned order."""

    coordinates = [
        f"{stop.latitude:.6f},{stop.longitude:.6f}" for stop in plan.stops
    ]
    depot = f"{DEPOT.latitude:.6f},{DEPOT.longitude:.6f}"
    return "https://www.google.com/maps/dir/?" + urlencode(
        {
            "api": "1",
            "origin": depot,
            "destination": depot,
            "travelmode": "driving",
            "waypoints": "|".join(coordinates),
        }
    )


def crew_route_csv(plan: SchedulePlan) -> bytes:
    """Export a crew run sheet with the planned sequence and coordinates."""

    rows = [
        {
            "stop": stop.sequence,
            "site_and_task": stop.job_name,
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


def render_route(plan: SchedulePlan) -> None:
    """Render a fitted, numbered visit sequence that is readable without hover."""

    route = [
        [DEPOT.longitude, DEPOT.latitude],
        *[[stop.longitude, stop.latitude] for stop in plan.stops],
        [DEPOT.longitude, DEPOT.latitude],
    ]
    stops = [
        {
            "position": [stop.longitude, stop.latitude],
            "sequence": stop.sequence,
            "marker": str(stop.sequence),
            "job_id": stop.job_id,
            "site": site_and_task(stop.job_name)[0],
            "time": (
                f"{minute_label(stop.start_minute)}–"
                f"{minute_label(stop.finish_minute)}"
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
            get_color=[8, 127, 91, 255],
            get_width=5,
            width_units="'pixels'",
        ),
        pdk.Layer(
            "ScatterplotLayer",
            [{"position": [DEPOT.longitude, DEPOT.latitude], "label": "Depot"}],
            get_position="position",
            get_radius=24,
            radius_units="'pixels'",
            get_fill_color=[30, 58, 95, 255],
            pickable=True,
        ),
        pdk.Layer(
            "ScatterplotLayer",
            stops,
            get_position="position",
            get_radius=17,
            radius_units="'pixels'",
            get_fill_color=[232, 89, 12, 255],
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
                    "position": [DEPOT.longitude, DEPOT.latitude],
                    "label": "DEPOT · START / FINISH",
                }
            ],
            get_position="position",
            get_text="label",
            get_size=12,
            size_units="'pixels'",
            get_color=[30, 58, 95, 255],
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
                "style": {"backgroundColor": "#17352c", "color": "white"},
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


def render_run_sheet(plan: SchedulePlan) -> None:
    """Render the operator-facing route, itinerary, and hand-off actions."""

    st.markdown("### Follow this route")
    st.caption(
        "The large numbers are the visit order. Stop 1 is where the shift begins."
    )
    directions_column, download_column = st.columns(2)
    with directions_column:
        st.link_button(
            "Open ordered stops in Google Maps",
            google_maps_route_url(plan),
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
        render_route(plan)
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
        decision_label = "Route confirmed"
        title = "Keep the current stop order"
        explanation = (
            "FortyGuard found nearly the same heat pattern across today's six "
            "sites. Reordering would not reduce heat enough to justify extra "
            f"travel. Start at <strong>{first_site}</strong> at "
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
    render_detail_sections(batch, jobs_frame, plans)


def render_result(batch: RealTemperatureBatch, jobs_frame: pd.DataFrame) -> None:
    """Turn a completed batch into separate crew and planner experiences."""

    jobs = build_domain_jobs(jobs_frame)
    try:
        plans = compare_schedules(
            jobs,
            batch.profiles,
            depot=DEPOT,
            uncertainty_penalty=0.0,
            heat_weight=HEAT_WEIGHT,
        )
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        st.error(
            "CertiRoute could not produce two comparable schedules while keeping "
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
        render_planner_panel(batch, jobs_frame, plans, reduction)
        return

    crew_plan = render_crew_decision(baseline, recommendation, reduction)
    render_run_sheet(crew_plan)
    render_safety_boundary(compact=True)


def render_detail_sections(
    batch: RealTemperatureBatch,
    jobs_frame: pd.DataFrame,
    plans: dict[ScheduleStrategy, SchedulePlan],
) -> None:
    """Keep inputs, provenance, implementation detail, and caveats secondary."""

    with st.expander("Review the six demonstration work orders"):
        display = jobs_frame.rename(
            columns={
                "job_id": "Work order",
                "name": "Site and fictional task",
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
                    "Site and fictional task",
                    "Minutes on site",
                    "Priority (5 highest)",
                    "Not before",
                    "Finish by",
                ]
            ],
            hide_index=True,
            width="stretch",
        )
        st.caption(
            "Landmarks are real. Tasks, priorities, durations, windows, and "
            "approximate service points are fictional demonstration inputs."
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


def render_empty_state(*, missing_count: int, key_available: bool) -> None:
    """Explain exactly what the primary action will produce."""

    st.markdown(
        """
        <div class="empty-state">
          <h3 style="margin-top:0">Your numbered crew route will appear here</h3>
          <p>Choose a replay date and build the route. You will get one clear
          decision, a numbered map, the first stop, stops 2–6, and the depot
          return time.</p>
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
            f"Ready to build. {10 - missing_count} of 10 hourly API responses "
            f"are already saved; the remaining {missing_count} will be retrieved."
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

jobs_frame = load_sample_jobs()
domain_jobs = build_domain_jobs(jobs_frame)
key_available = api_key_available()

st.markdown("## Set up the shift")
with st.container(border=True):
    summary_column, date_column, action_column = st.columns([1.55, 1, 1.05])
    with summary_column:
        st.markdown("**Phoenix field-service crew**")
        st.write("6 work orders · 08:00–17:00 · one crew · depot return required")
    with date_column:
        yesterday = date.today() - timedelta(days=1)
        selected_date = st.date_input(
            "Historical replay date",
            value=min(DEFAULT_REPLAY_DATE, yesterday),
            min_value=date(2021, 1, 1),
            max_value=yesterday,
            help="The current product slice replays a completed Phoenix workday.",
        )

    requests = build_profile_requests(
        domain_jobs,
        target_date=selected_date,
        sample_times=HOURLY_SAMPLE_TIMES,
        granularity=GRANULARITY_METRES,
    )
    area = polygon_area_square_miles(next(iter(requests.values())).polygon_aoi)
    oversized = area > DEFAULT_MAX_AOI_AREA_SQUARE_MILES
    store = HeatmapSnapshotStore(cache_path())
    with action_column:
        try:
            with st.spinner("Checking saved temperature evidence…"):
                collection_plan = plan_profile_collection(
                    requests,
                    store,
                    now_utc=datetime.now(UTC),
                )
        except CacheCorruptionError as exc:
            st.error(f"Saved FortyGuard evidence failed its integrity check: {exc}")
            st.stop()
        build_clicked = st.button(
            "Build crew route",
            type="primary",
            width="stretch",
            disabled=(
                oversized or (collection_plan.new_task_count > 0 and not key_available)
            ),
        )
        st.caption(
            f"{collection_plan.cache_hit_count}/10 temperature hours ready · "
            f"{area:.2f} mi² service area"
        )

if oversized:
    st.error(
        f"This {area:.2f} mi² scenario exceeds the "
        f"{DEFAULT_MAX_AOI_AREA_SQUARE_MILES:.0f} mi² request limit."
    )

request_key = (
    str(store.root),
    *(heatmap_request_fingerprint(request) for request in requests.values()),
)
batch: RealTemperatureBatch | None = None
if st.session_state.get("certiroute_request_key") == request_key:
    saved_batch = st.session_state.get("certiroute_temperature_batch")
    if isinstance(saved_batch, RealTemperatureBatch):
        batch = saved_batch

# A complete historical cache is safe to load without a network side effect.
if batch is None and not oversized and collection_plan.new_task_count == 0:
    try:
        batch = collect_batch(
            domain_jobs,
            collection_plan,
            store,
        )
    except (CacheCorruptionError, FortyGuardError, ValueError) as exc:
        st.error(f"Saved FortyGuard evidence could not be loaded: {exc}")

if build_clicked and not oversized:
    try:
        with st.status("Building a heat-aware workday…", expanded=True) as status:
            st.write("✓ Checked six jobs, deadlines, and the depot return")
            progress = st.progress(
                collection_plan.cache_hit_count / collection_plan.request_count,
                text=(
                    "Loading hourly FortyGuard temperatures · "
                    f"{collection_plan.cache_hit_count} of 10 already saved"
                ),
            )
            batch = collect_batch(
                domain_jobs,
                collection_plan,
                store,
            )
            progress.progress(1.0, text="Loaded 10 of 10 temperature hours")
            st.write("✓ Matched every work site to its returned temperature tile")
            st.write("✓ Compared feasible job orders and preserved every window")
            status.update(label="Heat-aware workday ready", state="complete")
    except (CacheCorruptionError, FortyGuardError, ValidationError, ValueError) as exc:
        batch = None
        st.error(f"The temperature lookup could not be completed: {exc}")
        st.info(
            "Any completed API responses were saved. Retry to continue from the "
            "remaining hours; no substitute data will be used."
        )

if batch is not None:
    st.session_state["certiroute_request_key"] = request_key
    st.session_state["certiroute_temperature_batch"] = batch
    render_result(batch, jobs_frame)
else:
    render_empty_state(
        missing_count=collection_plan.new_task_count,
        key_available=key_available,
    )
    st.markdown("---")
    render_safety_boundary(compact=True)
