"""Streamlit entry point for the first CertiRoute vertical slice."""

from datetime import date, time
from pathlib import Path

import pandas as pd
import pydeck as pdk
import streamlit as st
from pydantic import ValidationError

from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import (
    FortyGuardClient,
    HeatmapRequest,
    SingleHourDateTime,
    bounding_polygon,
    extract_temperature_stats,
)
from certiroute.fortyguard.errors import FortyGuardError
from certiroute.optimization import (
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
    compare_schedules,
)
from certiroute.risk import estimate_ambient_exposure
from certiroute.sample_conditions import build_demo_profile

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"


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


def build_demo_inputs(
    jobs: pd.DataFrame,
    *,
    certainty_overrides: dict[str, float] | None = None,
) -> tuple[list[Job], dict[str, TemperatureProfile]]:
    """Convert committed sample rows into the optimizer's domain models."""

    certainty_overrides = certainty_overrides or {}
    domain_jobs: list[Job] = []
    profiles: dict[str, TemperatureProfile] = {}
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
                "Temperature (°C)": stop.temperature_c,
                "Certainty": stop.certainty,
                "Raw units": stop.raw_exposure_units,
                "Adjusted units": stop.certainty_adjusted_units,
            }
            for stop in plan.stops
        ]
    )


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
            tooltip={"text": "Stop {sequence}"},
        ),
        use_container_width=True,
    )


def render_schedule_comparison(jobs: pd.DataFrame) -> None:
    """Show why decision optimization is different from another heat map."""

    scenario = st.radio(
        "Demonstration scenario",
        options=["Unfamiliar-condition stress test", "Normal conditions"],
        horizontal=True,
    )
    uncertainty_penalty = st.slider(
        "Uncertainty-aversion multiplier",
        min_value=0.0,
        max_value=2.0,
        value=1.0,
        step=0.25,
        help=(
            "A transparent planning preference, not a calibrated probability "
            "or regulatory value."
        ),
    )
    certainty_overrides = (
        {"PHX-101": 0.15} if scenario == "Unfamiliar-condition stress test" else None
    )
    if certainty_overrides:
        st.warning(
            "Stress test active: PHX-101 certainty is reduced from 94% to 15% "
            "to simulate a severe distribution shift. Compare Heat aware with "
            "Certainty aware to see whether the schedule changes."
        )

    domain_jobs, profiles = build_demo_inputs(
        jobs, certainty_overrides=certainty_overrides
    )
    depot = GeoPoint(latitude=33.44855, longitude=-112.07391)
    plans = compare_schedules(
        domain_jobs,
        profiles,
        depot=depot,
        uncertainty_penalty=uncertainty_penalty,
    )
    original = plans[ScheduleStrategy.ORIGINAL]
    recommended = plans[ScheduleStrategy.CERTAINTY_AWARE]
    reduction = 1 - (
        recommended.total_adjusted_exposure_units
        / original.total_adjusted_exposure_units
    )
    threshold_delta = (
        recommended.minutes_above_planning_threshold
        - original.minutes_above_planning_threshold
    )

    first, second, third, fourth = st.columns(4)
    first.metric(
        "Adjusted exposure reduction",
        f"{reduction:.1%}",
        help="Relative to the uploaded/original order in this synthetic scenario.",
    )
    second.metric(
        "Recommended travel",
        f"{recommended.total_travel_minutes} min",
        delta=(
            f"{recommended.total_travel_minutes - original.total_travel_minutes:+d} "
            "min vs original"
        ),
        delta_color="inverse",
    )
    third.metric(
        "High-screening-temperature work",
        f"{recommended.minutes_above_planning_threshold} min",
        delta=f"{threshold_delta:+d} min vs original",
        delta_color="inverse",
    )
    fourth.metric("Return to depot", minute_label(recommended.route_finish_minute))

    summary = pd.DataFrame(
        [
            {
                "Strategy": plan.strategy.value,
                "Job order": " → ".join(stop.job_id for stop in plan.stops),
                "Travel (min)": plan.total_travel_minutes,
                "Raw exposure": plan.total_raw_exposure_units,
                "Adjusted exposure": plan.total_adjusted_exposure_units,
                "Minutes ≥35 °C": plan.minutes_above_planning_threshold,
                "Depot return": minute_label(plan.route_finish_minute),
            }
            for plan in plans.values()
        ]
    )
    st.dataframe(summary, hide_index=True, use_container_width=True)

    selected_name = st.selectbox(
        "Inspect a schedule",
        options=[strategy.value for strategy in ScheduleStrategy],
        index=3,
    )
    selected_strategy = ScheduleStrategy(selected_name)
    selected_plan = plans[selected_strategy]
    map_column, table_column = st.columns([1, 1.35])
    with map_column:
        render_route(selected_plan, depot)
    with table_column:
        st.dataframe(
            schedule_rows(selected_plan),
            hide_index=True,
            use_container_width=True,
        )

    st.caption(
        "Sample mode uses a synthetic diurnal profile anchored to committed demo "
        "values. ‘35 °C’ is a configurable comparison threshold, not a universal "
        "occupational safety limit."
    )


def live_api_available() -> bool:
    """Check configuration without rendering or logging the secret."""

    try:
        settings = get_settings()
    except ValidationError:
        return False
    return bool(settings.fortyguard_api_key.get_secret_value().strip())


def render_sample_overview(jobs: pd.DataFrame) -> None:
    """Render the deterministic first slice of the product story."""

    total_raw = jobs["raw_exposure_units"].sum()
    total_adjusted = jobs["adjusted_exposure_units"].sum()
    least_certain = jobs.loc[jobs["sample_certainty"].idxmin()]

    first, second, third = st.columns(3)
    first.metric("Jobs", len(jobs))
    second.metric("Raw exposure units", f"{total_raw:.1f}")
    third.metric("Certainty-adjusted units", f"{total_adjusted:.1f}")

    st.map(jobs[["latitude", "longitude"]], zoom=13)
    st.dataframe(
        jobs[
            [
                "job_id",
                "name",
                "duration_minutes",
                "sample_temperature_c",
                "sample_certainty",
                "raw_exposure_units",
                "adjusted_exposure_units",
            ]
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.info(
        f"{least_certain['job_id']} has the lowest sample certainty "
        f"({least_certain['sample_certainty']:.0%}), so its adjusted planning "
        "score carries a larger conservative penalty."
    )
    st.caption(
        "One raw unit currently means one degree-Celsius hour above 27 °C. "
        "This transparent prototype score is not a medical heat-stress index."
    )


def render_live_heatmap(jobs: pd.DataFrame) -> None:
    """Allow an explicit, bounded live FortyGuard heatmap request."""

    st.subheader("FortyGuard live connection")
    if live_api_available():
        st.success("API configuration detected. The key remains server-side.")
    else:
        st.error("Add FORTYGUARD_API_KEY to .env before using live mode.")
        return

    st.warning(
        "A live submission can consume API credits after successful completion. "
        "Nothing is submitted until you press the button below."
    )
    date_column, time_column, granularity_column = st.columns(3)
    selected_date = date_column.date_input(
        "Heatmap date", value=date(2025, 7, 15), min_value=date(2021, 1, 1)
    )
    selected_time = time_column.time_input("Start time", value=time(14, 0))
    granularity = granularity_column.selectbox(
        "Granularity (metres)", options=[100, 80, 60], index=0
    )

    if st.button("Create one bounded heatmap", type="primary"):
        points = [
            GeoPoint(latitude=row.latitude, longitude=row.longitude)
            for row in jobs.itertuples(index=False)
        ]
        request = HeatmapRequest(
            polygon_aoi=bounding_polygon(points),
            date_time=SingleHourDateTime(
                start_date=selected_date,
                start_time=selected_time,
            ),
            granularity=granularity,
        )
        settings = get_settings()
        try:
            with st.spinner("FortyGuard is generating the heatmap..."):
                with FortyGuardClient(
                    api_key=settings.fortyguard_api_key,
                    base_url=settings.fortyguard_api_base_url,
                    timeout_seconds=settings.fortyguard_timeout_seconds,
                ) as client:
                    activity_id, result = client.create_heatmap(
                        request,
                        poll_interval_seconds=(
                            settings.fortyguard_poll_interval_seconds
                        ),
                        max_attempts=settings.fortyguard_max_poll_attempts,
                    )
        except FortyGuardError as exc:
            st.error(str(exc))
            return

        stats = extract_temperature_stats(result)
        st.session_state["last_activity_id"] = activity_id
        st.session_state["last_temperature_stats"] = stats.model_dump()
        st.success(f"Heatmap completed. Activity ID: {activity_id}")

    if "last_temperature_stats" in st.session_state:
        stats = st.session_state["last_temperature_stats"]
        columns = st.columns(4)
        labels = [
            ("Minimum", stats["minimum_c"]),
            ("Mean", stats["mean_c"]),
            ("Maximum", stats["maximum_c"]),
            ("Standard deviation", stats["standard_deviation_c"]),
        ]
        for column, (label, value) in zip(columns, labels, strict=True):
            column.metric(label, "Unavailable" if value is None else f"{value:.2f} °C")


st.set_page_config(page_title="CertiRoute", page_icon="🌡️", layout="wide")
st.title("CertiRoute")
st.caption("Certainty-aware heat-risk scheduling for mobile outdoor crews")

jobs_with_scores = add_exposure_scores(load_sample_jobs())
schedule_tab, overview_tab, live_tab, method_tab = st.tabs(
    ["Schedule comparison", "Sample data", "Live FortyGuard", "Method"]
)
with schedule_tab:
    render_schedule_comparison(jobs_with_scores)
with overview_tab:
    render_sample_overview(jobs_with_scores)
with live_tab:
    render_live_heatmap(jobs_with_scores)
with method_tab:
    st.markdown(
        """
        CertiRoute is a **forecast-based heat-risk planning and screening tool**.
        It compares the original order, an efficiency-only route, a point
        temperature heat-aware route, and a certainty-adjusted route.

        The prototype score is intentionally explainable. A sourced occupational
        heat-risk model and calibrated certainty method will replace it as richer
        environmental and field inputs become available.

        Ambient temperature alone cannot determine Heat Index, WBGT, workload,
        PPE burden, acclimatization, or individual susceptibility. The product
        therefore does not determine that work is safe, certify OSHA compliance,
        or replace an on-site effective-WBGT assessment and site-specific heat
        illness prevention plan.
        """
    )
