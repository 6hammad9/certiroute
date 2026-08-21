"""Streamlit entry point for the CertiRoute demonstration dashboard.

The page is written for a first-time visitor: it states the problem, shows one
recommendation in plain language, proves it with a time-of-day picture, and
only then exposes the underlying tables and controls.
"""

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
    cluster_points_into_aois,
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
    DEFAULT_REAL_SAMPLE_TIMES,
    RealTemperatureBatch,
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)
from certiroute.risk import estimate_ambient_exposure, relative_exposure_reduction
from certiroute.sample_conditions import build_demo_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
HEATMAP_CACHE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_heatmap_snapshots"

REAL_DATA_SOURCE = "FortyGuard API (real)"
SYNTHETIC_DATA_SOURCE = "Synthetic fallback"
SAMPLE_TIME_OPTIONS = {
    "3 samples — 08:00, 12:00, 17:00": DEFAULT_REAL_SAMPLE_TIMES,
    "5 samples — 08:00, 10:00, 12:00, 14:00, 17:00": (
        time(8),
        time(10),
        time(12),
        time(14),
        time(17),
    ),
    "10 hourly samples — 08:00–17:00": tuple(time(hour) for hour in range(8, 18)),
}

DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)
REFERENCE_TEMPERATURE_C = 27.0
PLANNING_THRESHOLD_C = 35.0
STRESS_TEST_JOB = "PHX-101"
STRESS_TEST_CERTAINTY = 0.15

# Plain-language names for the four strategies. The enum values are precise but
# read as internal vocabulary to anyone seeing the tool for the first time.
PLAN_LABELS = {
    ScheduleStrategy.ORIGINAL: "As dispatched",
    ScheduleStrategy.EFFICIENCY: "Standard route (shortest driving)",
    ScheduleStrategy.HEAT_AWARE: "Heat-aware",
    ScheduleStrategy.CERTAINTY_AWARE: "CertiRoute recommendation",
}
PLAN_EXPLANATIONS = {
    ScheduleStrategy.ORIGINAL: "The order the jobs arrived in.",
    ScheduleStrategy.EFFICIENCY: (
        "What an ordinary routing tool returns: least driving, heat ignored."
    ),
    ScheduleStrategy.HEAT_AWARE: "Avoids heat, but trusts every forecast equally.",
    ScheduleStrategy.CERTAINTY_AWARE: (
        "Avoids heat and hedges harder where the forecast is unreliable."
    ),
}
ANCHOR_DAY = datetime(2025, 7, 15)


@st.cache_data
def load_sample_jobs() -> pd.DataFrame:
    """Load the deterministic demonstration jobs committed with the project."""

    return pd.read_csv(SAMPLE_JOBS_PATH)


def add_exposure_scores(jobs: pd.DataFrame) -> pd.DataFrame:
    """Calculate the transparent planning score for every sample job."""

    scored = jobs.copy()
    estimates = [
        estimate_ambient_exposure(
            temperature_c=row.sample_temperature_c,
            duration_minutes=row.duration_minutes,
            certainty=row.sample_certainty,
        )
        for row in scored.itertuples(index=False)
    ]
    scored["raw_exposure_units"] = [item.raw_exposure_units for item in estimates]
    scored["adjusted_exposure_units"] = [
        item.certainty_adjusted_units for item in estimates
    ]
    return scored


def build_domain_jobs(jobs: pd.DataFrame) -> list[Job]:
    """Convert input rows into the optimizer's validated job models."""

    domain_jobs: list[Job] = []
    for row in jobs.itertuples(index=False):
        domain_jobs.append(
            Job(
                job_id=row.job_id,
                name=row.name,
                location=GeoPoint(
                    latitude=row.latitude,
                    longitude=row.longitude,
                ),
                duration_minutes=row.duration_minutes,
                priority=row.priority,
                earliest_start=time.fromisoformat(row.earliest_start),
                latest_finish=time.fromisoformat(row.latest_finish),
            )
        )
    return domain_jobs


def build_demo_inputs(
    jobs: pd.DataFrame,
    *,
    certainty_overrides: dict[str, float] | None = None,
) -> tuple[list[Job], dict[str, TemperatureProfile]]:
    """Build the explicitly synthetic fallback profiles."""

    certainty_overrides = certainty_overrides or {}
    domain_jobs = build_domain_jobs(jobs)
    profiles: dict[str, TemperatureProfile] = {}
    for row in jobs.itertuples(index=False):
        profiles[row.job_id] = build_demo_profile(
            job_id=row.job_id,
            anchor_temperature_c=row.sample_temperature_c,
            certainty=certainty_overrides.get(row.job_id, row.sample_certainty),
            diurnal_amplitude=row.diurnal_amplitude,
        )
    return domain_jobs, profiles


def minute_label(minute_of_day: int) -> str:
    """Format a minute offset as a compact 24-hour clock value."""

    hours, minutes = divmod(minute_of_day, 60)
    return f"{hours:02d}:{minutes:02d}"


def clock(minute_of_day: float) -> datetime:
    """Place a minute-of-day on the anchor date so charts get a time axis."""

    return ANCHOR_DAY + timedelta(minutes=float(minute_of_day))


def schedule_rows(plan: SchedulePlan) -> pd.DataFrame:
    """Build the operator-facing table for one schedule."""

    return pd.DataFrame(
        [
            {
                "Sequence": stop.sequence,
                "Job": stop.job_id,
                "Name": stop.job_name,
                "Arrive": minute_label(stop.arrival_minute),
                "Start": minute_label(stop.start_minute),
                "Finish": minute_label(stop.finish_minute),
                "Travel (min)": stop.inbound_travel_minutes,
                "Average (°C)": stop.temperature_c,
                "Peak (°C)": stop.peak_temperature_c,
                "Certainty": stop.certainty,
                "Raw units": stop.raw_exposure_units,
                "Adjusted units": stop.certainty_adjusted_units,
                "Minutes ≥35 °C": stop.minutes_above_planning_threshold,
            }
            for stop in plan.stops
        ]
    )


def timeline_frame(
    plans: dict[ScheduleStrategy, SchedulePlan],
    strategies: list[ScheduleStrategy],
) -> pd.DataFrame:
    """Flatten selected plans into one row per scheduled job for charting."""

    rows = []
    for strategy in strategies:
        for stop in plans[strategy].stops:
            rows.append(
                {
                    "Plan": PLAN_LABELS[strategy],
                    "Job": stop.job_id,
                    "Name": stop.job_name,
                    "Start": clock(stop.start_minute),
                    "Finish": clock(stop.finish_minute),
                    "Mid": clock((stop.start_minute + stop.finish_minute) / 2),
                    "Temperature": stop.temperature_c,
                    "Peak": stop.peak_temperature_c,
                    "Certainty": stop.certainty,
                    "Window": (
                        f"{minute_label(stop.start_minute)}"
                        f"–{minute_label(stop.finish_minute)}"
                    ),
                }
            )
    return pd.DataFrame(rows)


def render_timeline(
    frame: pd.DataFrame,
    lane_order: list[str],
    *,
    show_certainty: bool = True,
) -> None:
    """Draw the when-does-each-job-happen picture that carries the whole idea."""

    colour = alt.Color(
        "Temperature:Q",
        scale=alt.Scale(scheme="redyellowblue", reverse=True, domain=[28, 42]),
        legend=alt.Legend(
            title="Temperature while working (°C)", orient="top", gradientLength=220
        ),
    )
    tooltip = [
        alt.Tooltip("Name:N", title="Job"),
        alt.Tooltip("Window:N", title="Scheduled"),
        alt.Tooltip("Temperature:Q", title="Average °C", format=".1f"),
        alt.Tooltip("Peak:Q", title="Peak °C", format=".1f"),
    ]
    if show_certainty:
        tooltip.append(
            alt.Tooltip("Certainty:Q", title="Forecast certainty", format=".0%")
        )
    bars = (
        alt.Chart(frame)
        .mark_bar(cornerRadius=4, height=34, stroke="white", strokeWidth=1)
        .encode(
            x=alt.X(
                "Start:T",
                title="Time of day",
                axis=alt.Axis(format="%H:%M", tickCount=10, grid=True),
            ),
            x2="Finish:T",
            y=alt.Y("Plan:N", title=None, sort=lane_order),
            color=colour,
            tooltip=tooltip,
        )
    )
    labels = (
        alt.Chart(frame)
        .mark_text(fontSize=11, fontWeight="bold", color="white")
        .encode(
            x=alt.X("Mid:T"),
            y=alt.Y("Plan:N", sort=lane_order),
            text=alt.Text("Job:N"),
            tooltip=tooltip,
        )
    )
    st.altair_chart(
        (bars + labels).properties(height=90 + 46 * len(lane_order)),
        width="stretch",
    )


def temperature_curve_frame(
    profiles: dict[str, TemperatureProfile], jobs: pd.DataFrame
) -> pd.DataFrame:
    """Sample every site's profile across the shift for the explanatory chart."""

    names = dict(zip(jobs["job_id"], jobs["name"], strict=True))
    rows = []
    for job_id, profile in profiles.items():
        for minute in range(8 * 60, 17 * 60 + 1, 15):
            temperature, _ = profile.condition_at(minute)
            rows.append(
                {
                    "Job": job_id,
                    "Site": f"{job_id} · {names.get(job_id, job_id)}",
                    "Time": clock(minute),
                    "Temperature": round(temperature, 2),
                }
            )
    return pd.DataFrame(rows)


def render_temperature_curves(frame: pd.DataFrame) -> None:
    """Show why the time of day matters more than the distance between sites."""

    chart = (
        alt.Chart(frame)
        .mark_line(strokeWidth=2.5)
        .encode(
            x=alt.X("Time:T", title="Time of day", axis=alt.Axis(format="%H:%M")),
            y=alt.Y(
                "Temperature:Q",
                title="Temperature (°C)",
                scale=alt.Scale(zero=False),
            ),
            color=alt.Color("Site:N", title="Job site"),
            tooltip=[
                alt.Tooltip("Site:N"),
                alt.Tooltip("Time:T", format="%H:%M"),
                alt.Tooltip("Temperature:Q", format=".1f"),
            ],
        )
        .properties(height=320)
    )
    st.altair_chart(chart, width="stretch")


def biggest_improvement(
    baseline: SchedulePlan, recommended: SchedulePlan
) -> dict | None:
    """Find the single job whose move best explains the recommendation."""

    baseline_stops = {stop.job_id: stop for stop in baseline.stops}
    best = None
    for stop in recommended.stops:
        prior = baseline_stops.get(stop.job_id)
        if prior is None:
            continue
        cooler_by = prior.temperature_c - stop.temperature_c
        if cooler_by <= 0.05:
            continue
        if best is None or cooler_by > best["cooler_by"]:
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


def render_headline(
    baseline: SchedulePlan, recommended: SchedulePlan, reduction: float | None
) -> None:
    """State the recommendation as a sentence before showing any numbers."""

    extra_travel = recommended.total_travel_minutes - baseline.total_travel_minutes
    move = biggest_improvement(baseline, recommended)

    if move is None:
        st.info(
            "**No reordering needed today.** On this job set the heat-aware plan "
            "is already the efficient one, so the crew keeps the standard route."
        )
        return

    saved = (
        ""
        if reduction is None
        else f" cutting the crew's modelled heat load by **{reduction:.0%}**"
    )
    cost = (
        "at no extra driving"
        if extra_travel <= 0
        else f"for **{extra_travel} more minutes** of driving"
    )
    st.success(
        f"**Move {move['name']} ({move['job_id']}) from "
        f"{move['from_time']} to {move['to_time']}.** The crew works it at "
        f"**{move['to_temp']:.0f} °C instead of {move['from_temp']:.0f} °C**"
        f"{saved} — {cost}. Same five jobs, same shift, different order."
    )


def render_real_temperature_controls(
    domain_jobs: list[Job],
) -> RealTemperatureBatch | None:
    """Collect or reuse the exact real heatmaps that drive a schedule."""

    st.caption(
        "Historical replay using per-tile temperatures returned by FortyGuard. "
        "No API request is made until you explicitly authorize the missing tasks."
    )
    date_column, sample_column, granularity_column = st.columns([1, 1.6, 1])
    yesterday = date.today() - timedelta(days=1)
    selected_date = date_column.date_input(
        "Historical replay date",
        value=min(date(2026, 7, 15), yesterday),
        min_value=date(2021, 1, 1),
        max_value=yesterday,
        help=(
            "This first real-data workflow is a historical replay. Current-day "
            "and 12-hour forecast scheduling will use the same adapter next."
        ),
    )
    sample_option = sample_column.selectbox(
        "Temperature samples",
        options=list(SAMPLE_TIME_OPTIONS),
        index=0,
        help=(
            "Each sample is one single-hour heatmap task. Temperatures between "
            "samples are linearly interpolated for interval scoring."
        ),
    )
    granularity = granularity_column.selectbox(
        "Map granularity (metres)", options=[100, 80, 60], index=0
    )
    sample_times = SAMPLE_TIME_OPTIONS[sample_option]

    requests = build_profile_requests(
        domain_jobs,
        target_date=selected_date,
        sample_times=sample_times,
        granularity=granularity,
    )
    planned_polygon = next(iter(requests.values())).polygon_aoi
    planned_area = polygon_area_square_miles(planned_polygon)
    oversized = planned_area > DEFAULT_MAX_AOI_AREA_SQUARE_MILES
    if oversized:
        clusters = cluster_points_into_aois(
            [job.location for job in domain_jobs],
            max_area_square_miles=DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
        )
        st.error(
            f"These jobs require {len(clusters)} bounded AOIs. Multi-AOI real "
            "collection is not enabled yet, so no request can be submitted."
        )

    store = HeatmapSnapshotStore(HEATMAP_CACHE_PATH)
    try:
        collection_plan = plan_profile_collection(
            requests,
            store,
            now_utc=datetime.now(UTC),
        )
    except CacheCorruptionError as exc:
        st.error(f"The local FortyGuard snapshot cache failed integrity checks: {exc}")
        return None

    st.caption(
        f"Exact request plan: **{collection_plan.request_count} heatmap tasks** "
        f"for one {planned_area:.2f} mi² AOI — "
        f"{collection_plan.cache_hit_count} reusable local snapshots and "
        f"{collection_plan.new_task_count} new credit-consuming tasks."
    )
    request_key = tuple(
        heatmap_request_fingerprint(request) for request in requests.values()
    )
    batch = None
    if st.session_state.get("real_temperature_request_key") == request_key:
        candidate = st.session_state.get("real_temperature_batch")
        if isinstance(candidate, RealTemperatureBatch):
            batch = candidate

    if batch is None and collection_plan.new_task_count == 0:
        try:
            batch = collect_real_temperature_batch(
                domain_jobs,
                requests,
                store,
                client=None,
                max_new_tasks=0,
                now_utc=datetime.now(UTC),
            )
        except (CacheCorruptionError, FortyGuardError, ValueError) as exc:
            st.error(f"The cached real-temperature profile could not be loaded: {exc}")
            return None

    if batch is None and collection_plan.new_task_count:
        api_available = live_api_available()
        if not api_available:
            st.error(
                "A FortyGuard API key is required for the missing samples. Add "
                "FORTYGUARD_API_KEY to .env, or choose Synthetic fallback."
            )
        authorization = st.checkbox(
            "I authorize up to "
            f"{collection_plan.new_task_count} new credit-consuming FortyGuard "
            "heatmap tasks for this exact date, AOI and sampling plan."
        )
        submit = st.button(
            f"Fetch {collection_plan.new_task_count} missing real heatmaps",
            type="primary",
            disabled=not authorization or not api_available or oversized,
        )
        if submit:
            settings = get_settings()
            try:
                with st.spinner(
                    "Collecting the confirmed heatmaps. Completed samples are "
                    "cached so an interrupted batch can resume safely..."
                ):
                    with FortyGuardClient(
                        api_key=settings.fortyguard_api_key,
                        base_url=settings.fortyguard_api_base_url,
                        timeout_seconds=settings.fortyguard_timeout_seconds,
                    ) as client:
                        batch = collect_real_temperature_batch(
                            domain_jobs,
                            requests,
                            store,
                            client=client,
                            poll_interval_seconds=(
                                settings.fortyguard_poll_interval_seconds
                            ),
                            max_attempts=settings.fortyguard_max_poll_attempts,
                            max_new_tasks=collection_plan.new_task_count,
                            now_utc=datetime.now(UTC),
                        )
            except (CacheCorruptionError, FortyGuardError, ValueError) as exc:
                st.error(str(exc))
                st.info(
                    "Any samples that completed before the failure were saved. "
                    "Rerun the page to see the reduced remaining task count."
                )
                return None

    if batch is None:
        st.info(
            "Authorize and fetch the missing heatmaps above. Until then, no "
            "schedule is presented as real-data-driven."
        )
        return None

    st.session_state["real_temperature_request_key"] = request_key
    st.session_state["real_temperature_batch"] = batch
    st.success(
        "The schedule below is driven by real FortyGuard per-tile API output, "
        "not the synthetic temperature curves."
    )
    with st.expander("Verify temperatures and API provenance", expanded=False):
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Requested hour": minute_label(sample.minute_of_day),
                        "Activity ID": sample.activity_id,
                        "Collected (UTC)": sample.collected_at_utc,
                        "Retrieval": (
                            "Local cache" if sample.cache_hit else "Fetched now"
                        ),
                    }
                    for sample in batch.samples
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Job": job_id,
                        **{
                            minute_label(point.minute_of_day): point.temperature_c
                            for point in profile.points
                        },
                    }
                    for job_id, profile in batch.profiles.items()
                ]
            ),
            hide_index=True,
            width="stretch",
        )
        st.caption(batch.request_time_assumption)
    return batch


def render_synthetic_plan_tab(jobs: pd.DataFrame) -> None:
    """The main narrative: situation, recommendation, proof, then detail."""

    with st.container(border=True):
        st.markdown("#### 1 · Choose the day's conditions")
        left, right = st.columns([1.15, 1])
        with left:
            scenario = st.radio(
                "Which situation are we planning for?",
                options=["Unusual conditions", "Ordinary summer day"],
                horizontal=True,
                help=(
                    "Unusual conditions simulate a day when the temperature "
                    "forecast for one site becomes unreliable."
                ),
            )
        with right:
            uncertainty_penalty = st.slider(
                "How cautious should we be when a forecast looks unreliable?",
                min_value=0.0,
                max_value=2.0,
                value=1.0,
                step=0.25,
                help=(
                    "0 ignores forecast reliability entirely. 2 plans as though an "
                    "unreliable site could be far hotter than predicted. This is a "
                    "planning preference, not a calibrated probability."
                ),
            )

    unusual = scenario == "Unusual conditions"
    certainty_overrides = {STRESS_TEST_JOB: STRESS_TEST_CERTAINTY} if unusual else None
    if unusual:
        st.caption(
            f"⚠️ Simulated shift: the forecast for **{STRESS_TEST_JOB}** has dropped "
            f"from 94% to {STRESS_TEST_CERTAINTY:.0%} confidence. Its predicted "
            "temperature has not changed — only our trust in it."
        )

    domain_jobs, profiles = build_demo_inputs(
        jobs, certainty_overrides=certainty_overrides
    )
    try:
        plans = compare_schedules(
            domain_jobs,
            profiles,
            depot=DEPOT,
            uncertainty_penalty=uncertainty_penalty,
        )
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        st.error(f"A comparable four-plan result could not be produced: {exc}")
        st.info(
            "Check the original order and time windows. For larger job sets, "
            "increasing the search width may also find a feasible branch."
        )
        return

    baseline = plans[ScheduleStrategy.EFFICIENCY]
    recommended = plans[ScheduleStrategy.CERTAINTY_AWARE]
    reduction = relative_exposure_reduction(
        baseline.total_adjusted_exposure_units,
        recommended.total_adjusted_exposure_units,
    )

    st.markdown("#### 2 · What CertiRoute recommends")
    render_headline(baseline, recommended, reduction)

    first, second, third, fourth = st.columns(4)
    first.metric(
        "Heat exposure avoided",
        "—" if reduction is None else f"{reduction:.0%}",
        help=(
            "Reduction in the crew's modelled heat load compared with the "
            "shortest-driving route. Zero exposure on both plans shows as —."
        ),
    )
    second.metric(
        "Extra driving",
        f"{recommended.total_travel_minutes - baseline.total_travel_minutes:+d} min",
        help="The operational price of the safer ordering.",
    )
    threshold_delta = (
        recommended.minutes_above_planning_threshold
        - baseline.minutes_above_planning_threshold
    )
    third.metric(
        "Work above 35 °C",
        f"{recommended.minutes_above_planning_threshold:.0f} min",
        delta=f"{threshold_delta:+.0f} min",
        delta_color="inverse",
        help=(
            "Minutes the crew spends working at or above a configurable 35 °C "
            "screening line. Not a regulatory limit."
        ),
    )
    fourth.metric(
        "Crew back at depot",
        minute_label(recommended.route_finish_minute),
        help="Every plan must finish inside the same shift.",
    )

    st.markdown("#### 3 · Why — the same jobs, moved through the day")
    lane_order = [
        PLAN_LABELS[ScheduleStrategy.EFFICIENCY],
        PLAN_LABELS[ScheduleStrategy.CERTAINTY_AWARE],
    ]
    render_timeline(
        timeline_frame(
            plans, [ScheduleStrategy.EFFICIENCY, ScheduleStrategy.CERTAINTY_AWARE]
        ),
        lane_order,
    )
    st.caption(
        "Each block is one job, positioned at the time it is scheduled and "
        "coloured by the temperature the crew works in. **Red blocks moving left "
        "into cooler blue is the entire product.** Hover any block for detail."
    )

    st.markdown("#### 4 · All four plans compared")
    summary = pd.DataFrame(
        [
            {
                "Plan": PLAN_LABELS[plan.strategy],
                "What it optimises": PLAN_EXPLANATIONS[plan.strategy],
                "Job order": " → ".join(stop.job_id for stop in plan.stops),
                "Driving (min)": plan.total_travel_minutes,
                "Heat load": plan.total_raw_exposure_units,
                "Heat load, caution applied": plan.total_adjusted_exposure_units,
                "Min ≥35 °C": plan.minutes_above_planning_threshold,
                "Back at": minute_label(plan.route_finish_minute),
            }
            for plan in plans.values()
        ]
    )
    st.dataframe(
        summary,
        hide_index=True,
        width="stretch",
        column_config={
            "Heat load": st.column_config.NumberColumn(
                help=(
                    "Degree-hours above 27 °C accumulated across the shift. "
                    "Lower is better; the unit is explained under 'How it works'."
                ),
                format="%.1f",
            ),
            "Heat load, caution applied": st.column_config.NumberColumn(
                help=(
                    "The same figure after inflating sites whose forecast is "
                    "unreliable. This is what CertiRoute minimises."
                ),
                format="%.1f",
            ),
            "Min ≥35 °C": st.column_config.NumberColumn(format="%.0f"),
        },
    )

    with st.expander("Inspect one plan in detail — map, timings and per-job figures"):
        selected_label = st.selectbox(
            "Plan to inspect",
            options=[PLAN_LABELS[strategy] for strategy in ScheduleStrategy],
            index=3,
        )
        selected_strategy = next(
            strategy
            for strategy in ScheduleStrategy
            if PLAN_LABELS[strategy] == selected_label
        )
        selected_plan = plans[selected_strategy]
        st.caption(PLAN_EXPLANATIONS[selected_strategy])
        map_column, table_column = st.columns([1, 1.35])
        with map_column:
            render_route(selected_plan, DEPOT)
        with table_column:
            st.dataframe(schedule_rows(selected_plan), hide_index=True, width="stretch")

    st.caption(
        "Demonstration data: temperature profiles are synthetic and the certainty "
        "figures are authored inputs, not yet measured. The scheduling, exposure "
        "integration and comparison are real. See 'How it works & limits'."
    )


def real_schedule_rows(plan: SchedulePlan) -> pd.DataFrame:
    """Build a per-job table without displaying the neutral certainty sentinel."""

    return pd.DataFrame(
        [
            {
                "Sequence": stop.sequence,
                "Job": stop.job_id,
                "Name": stop.job_name,
                "Arrive": minute_label(stop.arrival_minute),
                "Start": minute_label(stop.start_minute),
                "Finish": minute_label(stop.finish_minute),
                "Travel (min)": stop.inbound_travel_minutes,
                "Average (°C)": stop.temperature_c,
                "Peak (°C)": stop.peak_temperature_c,
                "Reliability": "Not calibrated",
                "Raw units": stop.raw_exposure_units,
                "Minutes ≥35 °C": stop.minutes_above_planning_threshold,
            }
            for stop in plan.stops
        ]
    )


def render_real_plan_tab(jobs: pd.DataFrame) -> None:
    """Render a schedule whose temperatures come from FortyGuard tiles."""

    domain_jobs = build_domain_jobs(jobs)
    batch = render_real_temperature_controls(domain_jobs)
    if batch is None:
        return

    st.info(
        "Reliability is not calibrated yet. The optimizer applies no uncertainty "
        "penalty in this mode, so Certainty-aware intentionally matches Heat-aware; "
        "the neutral internal factor must not be read as 100% confidence."
    )
    try:
        plans = compare_schedules(
            domain_jobs,
            batch.profiles,
            depot=DEPOT,
            uncertainty_penalty=0.0,
        )
    except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
        st.error(f"A comparable four-plan result could not be produced: {exc}")
        return

    baseline = plans[ScheduleStrategy.EFFICIENCY]
    recommended = plans[ScheduleStrategy.HEAT_AWARE]
    reduction = relative_exposure_reduction(
        baseline.total_raw_exposure_units,
        recommended.total_raw_exposure_units,
    )

    st.markdown("#### 2 · What the real-temperature schedule recommends")
    render_headline(baseline, recommended, reduction)
    first, second, third, fourth = st.columns(4)
    first.metric(
        "Modeled heat exposure avoided",
        "—" if reduction is None else f"{reduction:.0%}",
        help="Compared with the shortest-driving route using FortyGuard temperatures.",
    )
    second.metric(
        "Extra driving",
        f"{recommended.total_travel_minutes - baseline.total_travel_minutes:+d} min",
    )
    threshold_delta = (
        recommended.minutes_above_planning_threshold
        - baseline.minutes_above_planning_threshold
    )
    third.metric(
        "Work above 35 °C",
        f"{recommended.minutes_above_planning_threshold:.0f} min",
        delta=f"{threshold_delta:+.0f} min",
        delta_color="inverse",
        help="A configurable comparison line, not a regulatory safety limit.",
    )
    fourth.metric("Crew back at depot", minute_label(recommended.route_finish_minute))

    st.markdown("#### 3 · The same real heat tiles, translated into a decision")
    compared_strategies = [
        ScheduleStrategy.EFFICIENCY,
        ScheduleStrategy.HEAT_AWARE,
    ]
    render_timeline(
        timeline_frame(plans, compared_strategies),
        [PLAN_LABELS[strategy] for strategy in compared_strategies],
        show_certainty=False,
    )
    st.caption(
        "Each block uses the linearly interpolated FortyGuard tile temperatures "
        "shown in the provenance panel above."
    )

    st.markdown("#### 4 · All four plans compared")
    summary = pd.DataFrame(
        [
            {
                "Plan": PLAN_LABELS[plan.strategy],
                "What it optimises": (
                    "Same as Heat-aware until reliability is calibrated."
                    if plan.strategy is ScheduleStrategy.CERTAINTY_AWARE
                    else PLAN_EXPLANATIONS[plan.strategy]
                ),
                "Job order": " → ".join(stop.job_id for stop in plan.stops),
                "Driving (min)": plan.total_travel_minutes,
                "Heat load": plan.total_raw_exposure_units,
                "Min ≥35 °C": plan.minutes_above_planning_threshold,
                "Back at": minute_label(plan.route_finish_minute),
            }
            for plan in plans.values()
        ]
    )
    st.dataframe(summary, hide_index=True, width="stretch")

    with st.expander("Inspect one real-data plan in detail"):
        selected_label = st.selectbox(
            "Plan to inspect",
            options=[PLAN_LABELS[strategy] for strategy in ScheduleStrategy],
            index=2,
            key="real_plan_to_inspect",
        )
        selected_strategy = next(
            strategy
            for strategy in ScheduleStrategy
            if PLAN_LABELS[strategy] == selected_label
        )
        selected_plan = plans[selected_strategy]
        map_column, table_column = st.columns([1, 1.35])
        with map_column:
            render_route(selected_plan, DEPOT)
        with table_column:
            st.dataframe(
                real_schedule_rows(selected_plan), hide_index=True, width="stretch"
            )

    st.caption(
        f"REAL TEMPERATURE SOURCE · FortyGuard Temperature API · "
        f"{batch.target_date.isoformat()} · {batch.granularity} m tiles. Job and "
        "travel inputs remain the committed demonstration scenario."
    )


def render_plan_tab(jobs: pd.DataFrame) -> None:
    """Choose between the real-data workflow and the offline fallback."""

    with st.container(border=True):
        st.markdown("#### 1 · Choose the temperature evidence")
        source = st.radio(
            "Temperature source",
            options=[REAL_DATA_SOURCE, SYNTHETIC_DATA_SOURCE],
            horizontal=True,
            help=(
                "Real mode maps each job to FortyGuard tiles. Synthetic fallback "
                "keeps the optimizer demonstrable without network access."
            ),
        )
    if source == REAL_DATA_SOURCE:
        render_real_plan_tab(jobs)
    else:
        render_synthetic_plan_tab(jobs)


def render_route(plan: SchedulePlan, depot: GeoPoint) -> None:
    """Render the selected job order as a route-like planning schematic."""

    route = [
        [depot.longitude, depot.latitude],
        *[[stop.longitude, stop.latitude] for stop in plan.stops],
        [depot.longitude, depot.latitude],
    ]
    path_layer = pdk.Layer(
        "PathLayer",
        [{"path": route}],
        get_path="path",
        get_color=[14, 116, 144],
        get_width=5,
        width_min_pixels=3,
    )
    stop_layer = pdk.Layer(
        "ScatterplotLayer",
        [
            {
                "position": [stop.longitude, stop.latitude],
                "sequence": stop.sequence,
                "label": f"{stop.sequence}. {stop.job_id}",
            }
            for stop in plan.stops
        ],
        get_position="position",
        get_radius=55,
        get_fill_color=[234, 88, 12],
        pickable=True,
    )
    view_state = pdk.ViewState(
        latitude=sum(point[1] for point in route) / len(route),
        longitude=sum(point[0] for point in route) / len(route),
        zoom=13.5,
    )
    st.pydeck_chart(
        pdk.Deck(
            layers=[path_layer, stop_layer],
            initial_view_state=view_state,
            tooltip={"text": "{label}"},
        ),
        width="stretch",
    )


def live_api_available() -> bool:
    """Check configuration without rendering or logging the secret."""

    try:
        settings = get_settings()
    except ValidationError:
        return False
    return bool(settings.fortyguard_api_key.get_secret_value().strip())


def render_jobs_tab(jobs: pd.DataFrame) -> None:
    """Show the inputs, and the time-of-day spread that makes ordering matter."""

    st.markdown("#### The five demonstration jobs being scheduled")
    st.caption(
        "One crew, one shift, 08:00–17:00, starting and finishing at the depot. "
        "Every plan must complete all five inside their time windows. The job "
        "coordinates and constraints are demo inputs; use the plan tab for real "
        "FortyGuard temperatures."
    )

    display = jobs.rename(
        columns={
            "job_id": "Job",
            "name": "Site",
            "duration_minutes": "Minutes on site",
            "priority": "Priority",
            "earliest_start": "Not before",
            "latest_finish": "Finish by",
            "sample_temperature_c": "Peak-hour °C",
            "sample_certainty": "Forecast certainty",
        }
    )
    st.dataframe(
        display[
            [
                "Job",
                "Site",
                "Minutes on site",
                "Priority",
                "Not before",
                "Finish by",
                "Peak-hour °C",
                "Forecast certainty",
            ]
        ],
        hide_index=True,
        width="stretch",
        column_config={
            "Forecast certainty": st.column_config.ProgressColumn(
                help="How much the temperature estimate for this site can be trusted.",
                min_value=0.0,
                max_value=1.0,
                format="%.0f%%",
            ),
            "Priority": st.column_config.NumberColumn(
                help="5 is most important. Higher priority work is pulled earlier."
            ),
        },
    )

    st.markdown("#### Synthetic fallback profile shape")
    _, profiles = build_demo_inputs(jobs)
    render_temperature_curves(temperature_curve_frame(profiles, jobs))
    st.caption(
        "The sites do not heat up alike. Chase Field climbs roughly 12 °C across "
        "the morning while City Hall barely moves, so the same 40 minutes of work "
        "costs several times more heat depending only on when it is scheduled. "
        "That gap is what the scheduler harvests."
    )

    st.markdown("#### Where they are")
    st.map(jobs[["latitude", "longitude"]], zoom=13)


def render_method_tab() -> None:
    """Explain the mechanism and state the boundaries in plain language."""

    st.markdown(
        """
        #### What the score means

        Heat load is measured in **degree-hours above 27 °C**. A crew working
        one hour at 37 °C accumulates 10; the same hour at 29 °C accumulates 2.
        Lower is better. It is deliberately simple so you can check it by hand.

        Where a site's forecast is unreliable, that figure is inflated before
        the optimiser sees it, so an untrustworthy site is treated as though it
        might be hotter than predicted. How much is the caution slider.

        #### How a plan is chosen

        Each candidate ordering is simulated minute by minute: driving between
        sites, waiting for time windows to open, and the temperature curve
        integrated across the exact minutes the crew is on site. The four plans
        differ only in what they are told to minimise.

        #### What is real today, and what is not

        | Part | Status |
        | --- | --- |
        | Scheduling, exposure integration, plan comparison | Real and tested |
        | FortyGuard API connection | Real, verified against the live service |
        | Temperature curves in real mode | **Real per-job FortyGuard tiles** |
        | Offline fallback curves | **Synthetic and explicitly labelled** |
        | Forecast certainty figures | **Authored inputs, not yet measured** |

        The remaining research work is a certainty score calibrated against
        archived forecast/realization error. Until then, real mode does not show
        or apply a confidence claim.

        #### Limits — please read

        CertiRoute is **decision support for planning, not a safety
        determination**. Ambient temperature alone cannot establish Heat Index
        or WBGT, and it knows nothing about humidity, radiant load, workload,
        PPE burden, acclimatisation, or individual susceptibility.

        It does not determine that work is safe, certify OSHA compliance, or
        replace an on-site WBGT assessment, a heat illness prevention plan, or
        qualified safety advice. The 35 °C line used above is a configurable
        comparison threshold, not a regulatory limit. Symptoms always override
        any score on this page.
        """
    )


st.set_page_config(page_title="CertiRoute", page_icon="🌡️", layout="wide")

st.title("🌡️ CertiRoute")
st.subheader("Schedule outdoor crews around the heat, not just the map")

with st.container(border=True):
    intro, steps = st.columns([1.4, 1])
    with intro:
        st.markdown(
            """
            **The problem.** A field crew's jobs can be done in any order that
            respects their time windows. Ordinary routing tools pick the order
            with the least driving — and unknowingly park workers at the hottest
            site during the hottest hour. The same 40-minute job can cost
            **six times more heat exposure** at 2pm than at 8am.

            **What this does.** It reorders the day so the hot work happens in
            the cool hours, tells you exactly what that costs in extra driving,
            and hedges harder wherever the temperature forecast is unreliable.
            """
        )
    with steps:
        st.markdown(
            """
            **How to read this page**

            1. Pick the day's conditions →
            2. Read the one-line recommendation
            3. Check the coloured timeline — red blocks moving into the cool
               morning is the whole idea
            4. Compare all four plans in the table
            """
        )

jobs_with_scores = add_exposure_scores(load_sample_jobs())
plan_tab, jobs_tab, live_tab, method_tab = st.tabs(
    [
        "📋 The plan",
        "🧰 Today's jobs",
        "🛰️ Live temperature data",
        "📖 How it works & limits",
    ]
)
with plan_tab:
    render_plan_tab(jobs_with_scores)
with jobs_tab:
    render_jobs_tab(jobs_with_scores)
with live_tab:
    st.markdown("#### Real temperature data now drives the schedule")
    st.info(
        "Open **The plan** and choose **FortyGuard API (real)**. That workflow "
        "maps each job to returned temperature tiles, shows the exact task count "
        "before submission, caches completed results, and displays provenance."
    )
with method_tab:
    render_method_tab()
