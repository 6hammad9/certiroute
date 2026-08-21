"""One-page Streamlit interface for the CertiRoute hackathon product.

The product path is deliberately simple: review one fictional workday, load
real FortyGuard temperature evidence, and compare an operations baseline with
a heat-aware schedule. Technical provenance and limitations remain available
below the decision instead of competing with it.
"""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import altair as alt
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
    TemperatureProfile,
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
ANCHOR_DAY = datetime(2026, 7, 15)

PLAN_LABELS = {
    ScheduleStrategy.EFFICIENCY: "Distance-efficient operations baseline",
    ScheduleStrategy.HEAT_AWARE: "Heat-aware recommendation",
}
TIMELINE_LABELS = {
    ScheduleStrategy.EFFICIENCY: "Operations baseline",
    ScheduleStrategy.HEAT_AWARE: "Heat-aware plan",
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


def chart_clock(minute_of_day: int | float) -> datetime:
    """Place a minute-of-day on an arbitrary anchor date for Altair."""

    return ANCHOR_DAY + timedelta(minutes=float(minute_of_day))


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
            color: #334155; font-size: 1.12rem; line-height: 1.6;
            max-width: 820px; margin-top: -0.5rem;
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
        .step-number {
            display: inline-flex; align-items: center; justify-content: center;
            width: 1.7rem; height: 1.7rem; border-radius: 50%;
            color: white; background: #087f5b; font-weight: 800;
            margin-right: .35rem;
        }
        .step-copy { color: #475569; line-height: 1.45; }
        .empty-state {
            text-align: center; padding: 1.7rem 1rem; border: 1px dashed #9fb7af;
            border-radius: .75rem; background: #fbfdfc;
        }
        .safety-note {
            border-left: 4px solid #d97706; background: #fff9ed;
            padding: .85rem 1rem; border-radius: .35rem; color: #5f430b;
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
    st.subheader("Put outdoor work in cooler feasible hours.")
    st.markdown(
        """
        <div class="hero-copy">
        A fictional utility crew has six work orders at real Phoenix landmarks.
        CertiRoute uses hourly, street-level FortyGuard temperatures to ask a
        practical question: <strong>can the same work be moved—not
        cancelled—to reduce modeled ambient-heat load?</strong>
        </div>
        <div style="margin-top:.7rem">
          <span class="badge">DEMONSTRATION WORK ORDERS</span>
          <span class="badge">REAL FORTYGUARD TEMPERATURES</span>
          <span class="badge">HISTORICAL REPLAY</span>
          <span class="badge">NO SUBSTITUTE DATA</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_three_steps() -> None:
    """Give a first-time visitor a persistent three-step mental model."""

    st.markdown("### One workday, one clear decision")
    columns = st.columns(3)
    steps = (
        (
            "Review the work",
            "Six jobs keep their durations, priorities, deadlines, and depot return.",
        ),
        (
            "Load the heat",
            "Ten hourly FortyGuard heatmaps provide temperature at every site.",
        ),
        (
            "Read the trade-off",
            "See modeled exposure change, hot-work time, and added estimated travel.",
        ),
    )
    for index, (title, copy) in enumerate(steps, start=1):
        with columns[index - 1].container(border=True):
            st.markdown(
                f'<span class="step-number">{index}</span><strong>{title}</strong>',
                unsafe_allow_html=True,
            )
            st.markdown(f'<div class="step-copy">{copy}</div>', unsafe_allow_html=True)


def timeline_frame(plans: dict[ScheduleStrategy, SchedulePlan]) -> pd.DataFrame:
    """Flatten the two customer-facing plans for the schedule timeline."""

    rows: list[dict[str, object]] = []
    for strategy in (ScheduleStrategy.EFFICIENCY, ScheduleStrategy.HEAT_AWARE):
        for stop in plans[strategy].stops:
            rows.append(
                {
                    "Plan": PLAN_LABELS[strategy],
                    "Lane": TIMELINE_LABELS[strategy],
                    "Job": stop.job_id,
                    "Name": stop.job_name,
                    "Start": chart_clock(stop.start_minute),
                    "Finish": chart_clock(stop.finish_minute),
                    "Mid": chart_clock((stop.start_minute + stop.finish_minute) / 2),
                    "Temperature": stop.temperature_c,
                    "Peak": stop.peak_temperature_c,
                    "Window": (
                        f"{minute_label(stop.start_minute)}–"
                        f"{minute_label(stop.finish_minute)}"
                    ),
                }
            )
    return pd.DataFrame(rows)


def render_timeline(plans: dict[ScheduleStrategy, SchedulePlan]) -> None:
    """Show when every job happens and how hot it is while work occurs."""

    frame = timeline_frame(plans)
    lane_order = [
        TIMELINE_LABELS[ScheduleStrategy.EFFICIENCY],
        TIMELINE_LABELS[ScheduleStrategy.HEAT_AWARE],
    ]
    tooltip = [
        alt.Tooltip("Plan:N", title="Plan"),
        alt.Tooltip("Name:N", title="Work order"),
        alt.Tooltip("Window:N", title="Scheduled"),
        alt.Tooltip("Temperature:Q", title="Average °C", format=".1f"),
        alt.Tooltip("Peak:Q", title="Peak °C", format=".1f"),
    ]
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadius=5, height=34, stroke="white", strokeWidth=1)
        .encode(
            x=alt.X(
                "Start:T",
                title="Phoenix local shift time",
                axis=alt.Axis(format="%H:%M", tickCount=10, grid=True),
            ),
            x2="Finish:T",
            y=alt.Y("Lane:N", title=None, sort=lane_order),
            color=alt.Color(
                "Temperature:Q",
                scale=alt.Scale(
                    domain=[25, 35, 45],
                    range=["#2563eb", "#f59e0b", "#b91c1c"],
                ),
                legend=alt.Legend(
                    title="Ambient °C while working",
                    orient="top",
                    gradientLength=250,
                ),
            ),
            tooltip=tooltip,
        )
    )
    labels = (
        alt.Chart(frame)
        .mark_text(fontSize=11, fontWeight="bold", color="white")
        .encode(
            x=alt.X("Mid:T"),
            y=alt.Y("Lane:N", sort=lane_order),
            text=alt.Text("Job:N"),
            tooltip=tooltip,
        )
    )
    st.altair_chart((bars + labels).properties(height=190), width="stretch")


def biggest_improvement(
    baseline: SchedulePlan, recommendation: SchedulePlan
) -> dict[str, object] | None:
    """Return the moved job with the largest modeled cooling difference."""

    baseline_by_job = {stop.job_id: stop for stop in baseline.stops}
    best: dict[str, object] | None = None
    for stop in recommendation.stops:
        prior = baseline_by_job[stop.job_id]
        cooler_by = prior.temperature_c - stop.temperature_c
        if cooler_by <= 0.05:
            continue
        if best is None or cooler_by > float(best["cooler_by"]):
            best = {
                "job_id": stop.job_id,
                "name": stop.job_name,
                "from_time": minute_label(prior.start_minute),
                "to_time": minute_label(stop.start_minute),
                "from_temp": prior.temperature_c,
                "to_temp": stop.temperature_c,
                "cooler_by": cooler_by,
            }
    return best


def maximum_site_spread(profiles: dict[str, TemperatureProfile]) -> tuple[float, int]:
    """Return the largest same-hour temperature spread across all job sites."""

    temperatures_by_minute: dict[int, list[float]] = {}
    for profile in profiles.values():
        for point in profile.points:
            temperatures_by_minute.setdefault(point.minute_of_day, []).append(
                point.temperature_c
            )
    spreads = {
        minute: max(values) - min(values)
        for minute, values in temperatures_by_minute.items()
        if values
    }
    minute = max(spreads, key=spreads.get)
    return spreads[minute], minute


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


def render_route(plan: SchedulePlan) -> None:
    """Render the visit sequence as a schematic rather than road navigation."""

    route = [
        [DEPOT.longitude, DEPOT.latitude],
        *[[stop.longitude, stop.latitude] for stop in plan.stops],
        [DEPOT.longitude, DEPOT.latitude],
    ]
    path_layer = pdk.Layer(
        "PathLayer",
        [{"path": route}],
        get_path="path",
        get_color=[8, 127, 91],
        get_width=5,
        width_min_pixels=3,
    )
    stop_layer = pdk.Layer(
        "ScatterplotLayer",
        [
            {
                "position": [stop.longitude, stop.latitude],
                "label": f"{stop.sequence}. {stop.job_id}",
            }
            for stop in plan.stops
        ],
        get_position="position",
        get_radius=80,
        get_fill_color=[234, 88, 12],
        pickable=True,
    )
    st.pydeck_chart(
        pdk.Deck(
            layers=[path_layer, stop_layer],
            initial_view_state=pdk.ViewState(
                latitude=33.444,
                longitude=-112.015,
                zoom=11.3,
            ),
            tooltip={"text": "{label}"},
        ),
        width="stretch",
    )
    st.caption(
        "Lines show visit order only. Travel minutes use straight-line distance "
        "at an assumed average speed; this prototype is not turn-by-turn routing."
    )


def render_headline(
    baseline: SchedulePlan,
    recommendation: SchedulePlan,
    profiles: dict[str, TemperatureProfile],
    reduction: float | None,
) -> None:
    """State the operational decision before supporting charts or tables."""

    order_changed = [stop.job_id for stop in baseline.stops] != [
        stop.job_id for stop in recommendation.stops
    ]
    meaningful_reduction = reduction is not None and reduction >= 0.005
    extra_travel = recommendation.total_travel_minutes - baseline.total_travel_minutes
    if not order_changed or not meaningful_reduction:
        spread, minute = maximum_site_spread(profiles)
        st.info(
            "### Keep the distance-efficient route for this replay\n\n"
            "CertiRoute found no lower-heat ordering worth additional travel. "
            f"Across the six sites, the largest same-hour temperature spread was "
            f"**{spread:.2f} °C at {minute_label(minute)}**. This is a valid "
            "planning result, not a data failure."
        )
        return

    move = biggest_improvement(baseline, recommendation)
    travel_phrase = (
        "with no added estimated travel"
        if extra_travel <= 0
        else f"for **{extra_travel} added minutes** of estimated travel"
    )
    if move is None:
        st.success(
            "### Use the heat-aware order\n\n"
            f"It lowers modeled ambient-heat load by **{reduction:.1%}** "
            f"{travel_phrase}, while completing every job inside its window."
        )
        return

    st.success(
        f"### Start {move['name']} at {move['to_time']}\n\n"
        f"The largest improvement moves **{move['job_id']}** from "
        f"{move['from_time']} to {move['to_time']}, when its modeled ambient "
        f"temperature is **{float(move['cooler_by']):.1f} °C cooler**. The full "
        f"order lowers modeled ambient-heat load by **{reduction:.1%}** "
        f"{travel_phrase}."
    )


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


def render_result(batch: RealTemperatureBatch, jobs_frame: pd.DataFrame) -> None:
    """Turn a completed real-temperature batch into the one-page decision."""

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

    st.markdown("## The recommendation")
    render_headline(baseline, recommendation, batch.profiles, reduction)
    render_metrics(baseline, recommendation, reduction)

    schedules_match = [stop.job_id for stop in baseline.stops] == [
        stop.job_id for stop in recommendation.stops
    ]
    st.markdown(
        "### Why the schedule stays the same"
        if schedules_match
        else "### See what moved and why"
    )
    st.caption(
        "Each block is one job. Position shows when the crew works; color shows "
        "the FortyGuard ambient temperature at that site. Hover for exact values."
    )
    render_timeline(plans)

    st.markdown("### Recommended job sequence")
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
        recommendation = plans[ScheduleStrategy.HEAT_AWARE]
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
        render_route(recommendation)

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


def render_empty_state(*, missing_count: int, key_available: bool) -> None:
    """Explain exactly what the primary action will produce."""

    st.markdown(
        """
        <div class="empty-state">
          <h3 style="margin-top:0">Your recommendation will appear here</h3>
          <p>Select a replay date and build the plan. CertiRoute will load hourly
          temperature at every work site, compare feasible job orders, and show
          the heat benefit beside its travel cost.</p>
          <p><strong>Operations baseline:</strong> heat ignored &nbsp;·&nbsp;
          <strong>Heat-aware recommendation:</strong> ambient-temperature
          exposure added</p>
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


def render_safety_boundary() -> None:
    """Keep the responsible-use limit visible without hiding it in an expander."""

    st.markdown(
        """
        <div class="safety-note">
        <strong>Planning support, not safety clearance.</strong> This prototype
        models ambient temperature only. It does not account for humidity,
        radiant heat, workload, clothing or PPE, acclimatization, or worker
        health. The 35 °C comparison line is configurable and is not a
        regulatory limit. It does not determine that work is safe.
        </div>
        """,
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

st.markdown("## Plan this Phoenix shift")
with st.container(border=True):
    summary_column, date_column, action_column = st.columns([1.55, 1, 1.05])
    with summary_column:
        st.markdown("**Phoenix field-service crew**")
        st.write("6 work orders · 08:00–17:00 · one crew · depot return required")
        st.caption(
            "Real landmarks; fictional tasks and constraints. U.S. historical "
            "temperature replay."
        )
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
            "Build heat-aware schedule",
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
render_safety_boundary()
