"""UI contract tests for the real-data-only Streamlit experience."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

from certiroute.collection import HeatmapSnapshotStore, SnapshotTemporalScope
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.real_conditions import build_profile_requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "main.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
REPLAY_DATE = date(2026, 7, 15)
SAMPLE_TIMES = tuple(time(hour) for hour in range(8, 18))

# The last site heats rapidly in this deterministic API-shaped fixture. This
# gives the real-data UI a meaningful, reproducible scheduling trade-off.
SITE_CURVES = {
    "PHX-201": (29.0, 0.3),
    "PHX-202": (29.0, 0.5),
    "PHX-203": (30.0, 0.4),
    "PHX-204": (29.0, 0.8),
    "PHX-205": (31.0, 0.6),
    "PHX-206": (25.0, 2.0),
}


@dataclass(frozen=True)
class RenderedStates:
    """Network-isolated first-run, crew, and planner product states."""

    empty: AppTest
    crew: AppTest
    planner: AppTest
    network_calls: list[str]


def _load_jobs() -> list[Job]:
    frame = pd.read_csv(SAMPLE_PATH)
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


def _tile_result(jobs: list[Job], hour: int) -> dict[str, Any]:
    """Build a completed FortyGuard-shaped result covering every work site."""

    features: list[dict[str, Any]] = []
    half_width = 0.0002
    for job in jobs:
        base, hourly_rise = SITE_CURVES[job.job_id]
        temperature = base + hourly_rise * (hour - 8)
        longitude = job.location.longitude
        latitude = job.location.latitude
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "tile_id": f"fixture-{job.job_id}-{hour:02d}",
                    "average_temperature": temperature,
                },
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [longitude - half_width, latitude - half_width],
                            [longitude + half_width, latitude - half_width],
                            [longitude + half_width, latitude + half_width],
                            [longitude - half_width, latitude + half_width],
                            [longitude - half_width, latitude - half_width],
                        ]
                    ],
                },
            }
        )
    return {"map_data": {"type": "FeatureCollection", "features": features}}


def _publish_cached_replay(cache_root: Path) -> None:
    jobs = _load_jobs()
    requests = build_profile_requests(
        jobs,
        target_date=REPLAY_DATE,
        sample_times=SAMPLE_TIMES,
        granularity=60,
    )
    store = HeatmapSnapshotStore(cache_root)
    collected_at = datetime(2026, 8, 20, 12, tzinfo=UTC)
    for minute, request in requests.items():
        store.publish(
            request,
            activity_id=f"fixture-activity-{minute}",
            collected_at_utc=collected_at,
            temporal_scope=SnapshotTemporalScope.HISTORICAL,
            raw_result=_tile_result(jobs, minute // 60),
        )


def _run_app() -> AppTest:
    app = AppTest.from_file(APP_PATH)
    app.run(timeout=60)
    assert not app.exception
    return app


@pytest.fixture(scope="module")
def rendered_states(tmp_path_factory: pytest.TempPathFactory) -> RenderedStates:
    """Render empty and cached states while making network use impossible."""

    patch = pytest.MonkeyPatch()
    network_calls: list[str] = []

    def reject_network(*_args: object, **_kwargs: object) -> object:
        network_calls.append("create_heatmap")
        raise AssertionError("AppTest must not submit a FortyGuard request")

    patch.setenv("FORTYGUARD_API_KEY", "ui-test-key")
    patch.setattr(FortyGuardClient, "create_heatmap", reject_network)
    get_settings.cache_clear()

    empty_root = tmp_path_factory.mktemp("empty_heatmap_cache")
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(empty_root))
    empty = _run_app()

    cached_root = tmp_path_factory.mktemp("cached_heatmap_cache")
    _publish_cached_replay(cached_root)
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(cached_root))
    crew = _run_app()

    # Use an independent AppTest session for the planner contract so no state
    # from the crew view can make the separation test pass accidentally.
    planner = _run_app()
    planner.segmented_control(key="view_mode").select("Planner details").run(
        timeout=60
    )
    assert not planner.exception

    try:
        yield RenderedStates(
            empty=empty,
            crew=crew,
            planner=planner,
            network_calls=network_calls,
        )
    finally:
        get_settings.cache_clear()
        patch.undo()


def _all_text(app: AppTest) -> str:
    """Return prose from both the primary page and its detail expanders."""

    elements = [*app.title, *app.subheader, *app.markdown, *app.caption]
    elements += [*app.success, *app.info, *app.warning, *app.error]
    combined = " ".join(str(element.value) for element in elements)
    return " ".join(combined.split())


def _dataframe_with_column(app: AppTest, column: str) -> pd.DataFrame:
    return next(frame.value for frame in app.dataframe if column in frame.value.columns)


def test_empty_cache_is_a_guided_real_only_first_run(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.empty
    text = _all_text(app)

    assert app.title[0].value == "CertiRoute"
    assert "A clear, numbered work route built around Phoenix heat" in text
    assert "Choose date" in text
    assert "Build route" in text
    assert "Follow stops 1–6" in text
    assert "REAL FORTYGUARD TEMPERATURES" in text
    assert "Your numbered crew route will appear here" in text
    assert "a numbered map" in text
    assert "the first stop" in text
    assert "the depot return time" in text

    assert len(app.button) == 1
    assert app.button[0].label == "Build crew route"
    assert not app.segmented_control
    assert not app.radio
    assert not app.checkbox
    assert not app.metric
    assert not app.dataframe
    assert not app.expander
    assert not app.get("deck_gl_json_chart")
    assert "synthetic fallback" not in text.lower()
    assert rendered_states.network_calls == []


def test_crew_route_leads_with_one_plain_language_decision(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.crew
    text = _all_text(app)
    control = app.segmented_control(key="view_mode")

    assert control.options == ["Crew route", "Planner details"]
    assert control.value == "Crew route"
    assert "Route result" in text
    assert (
        "Keep the current stop order" in text or "Use this stop order" in text
    )
    assert "First stop" in text
    assert "Jobs on time" in text
    assert "Follow this route" in text
    assert "Stop 1 is where the shift begins" in text
    assert "Return to depot" in text
    assert rendered_states.network_calls == []


def test_crew_route_has_six_ordered_instruction_cards(
    rendered_states: RenderedStates,
) -> None:
    route_markup = next(
        element.value
        for element in rendered_states.crew.markdown
        if 'data-route-stop="1"' in str(element.value)
    )
    positions = [
        route_markup.index(f'data-route-stop="{stop}"') for stop in range(1, 7)
    ]

    assert positions == sorted(positions)
    assert route_markup.count('class="route-stop"') == 6
    assert route_markup.count('class="route-stop-number"') == 6
    assert route_markup.count("Start here") == 1
    assert route_markup.count("Next stop") == 5
    assert "Return to depot" in route_markup


def test_crew_route_map_makes_the_visit_order_visible(
    rendered_states: RenderedStates,
) -> None:
    charts = rendered_states.crew.get("deck_gl_json_chart")

    assert len(charts) == 1
    specification = json.loads(charts[0].proto.json)
    layers = specification["layers"]
    layer_types = [layer["@@type"] for layer in layers]

    assert layer_types.count("PathLayer") == 2
    assert layer_types.count("ScatterplotLayer") == 2
    assert layer_types.count("TextLayer") == 2

    route_layer = next(layer for layer in layers if layer["@@type"] == "PathLayer")
    assert len(route_layer["data"][0]["path"]) == 8  # depot + six stops + depot

    number_layer = next(
        layer
        for layer in layers
        if layer["@@type"] == "TextLayer" and layer.get("getText") == "@@=marker"
    )
    assert [row["marker"] for row in number_layer["data"]] == [
        "1",
        "2",
        "3",
        "4",
        "5",
        "6",
    ]


def test_crew_route_has_navigation_and_download_actions(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.crew
    links = app.get("link_button")

    assert len(links) == 1
    assert links[0].proto.label == "Open ordered stops in Google Maps"
    parsed = urlparse(links[0].proto.url)
    query = parse_qs(parsed.query)
    assert parsed.netloc == "www.google.com"
    assert parsed.path == "/maps/dir/"
    assert query["api"] == ["1"]
    assert query["origin"] == query["destination"]
    assert query["travelmode"] == ["driving"]
    assert len(query["waypoints"][0].split("|")) == 6

    assert len(app.download_button) == 1
    assert app.download_button[0].label == "Download crew route (CSV)"
    assert app.download_button[0].proto.url


def test_crew_route_hides_planner_complexity(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.crew
    text = _all_text(app)

    assert not app.metric
    assert not app.dataframe
    assert not app.expander
    assert not app.get("vega_lite_chart")
    assert "Modeled exposure" not in text
    assert "degree-hours" not in text
    assert "FortyGuard activity ID" not in text
    assert "Compare planning methods" not in text
    assert "Planning aid—not safety clearance" in text


def test_planner_view_owns_the_comparison_metrics_and_method_table(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.planner
    text = _all_text(app)
    control = app.segmented_control(key="view_mode")
    metrics = {metric.label: metric.value for metric in app.metric}

    assert control.value == "Planner details"
    assert "Planner details" in text
    assert "For dispatchers, reviewers, and judges" in text
    assert set(metrics) == {
        "Modeled exposure",
        "Hot-work time ≥35 °C",
        "Added estimated travel",
        "Jobs completed on time",
    }
    assert metrics["Jobs completed on time"] == "6 / 6"

    methods = _dataframe_with_column(app, "Plan")
    assert list(methods["Plan"]) == [
        "Distance-efficient operations baseline",
        "Heat-aware recommendation",
    ]
    assert "What it balances" in methods.columns
    assert "Modeled exposure" in methods.columns


def test_planner_view_provides_exact_scheduled_conditions(
    rendered_states: RenderedStates,
) -> None:
    sequence = _dataframe_with_column(rendered_states.planner, "Stop")

    assert len(sequence) == 6
    assert list(sequence["Stop"]) == [1, 2, 3, 4, 5, 6]
    assert list(sequence.columns) == [
        "Stop",
        "Work order",
        "Site and task",
        "Start",
        "Finish",
        "Ambient temperature",
        "Change from baseline",
    ]
    assert sequence["Work order"].is_unique
    assert sequence["Ambient temperature"].str.endswith(" °C").all()


def test_planner_view_exposes_auditable_api_evidence(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.planner
    text = _all_text(app)
    temperatures = _dataframe_with_column(app, "08:00")
    source_records = _dataframe_with_column(app, "FortyGuard activity ID")

    assert "FortyGuard Temperature API" in text
    assert "10 hourly heatmaps" in text
    assert "60 m" in text
    assert "No synthetic or substitute temperature profile" in text
    assert len(temperatures) == 6
    assert list(temperatures.columns[1:]) == [f"{hour:02d}:00" for hour in range(8, 18)]
    assert len(source_records) == 10
    assert (source_records["Retrieved"] == "Saved API response").all()
    assert (
        source_records["FortyGuard activity ID"]
        .str.startswith("fixture-activity-")
        .all()
    )


def test_planner_view_keeps_methods_and_full_safety_detail_secondary(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.planner
    text = _all_text(app)
    expander_labels = [expander.label for expander in app.expander]

    assert expander_labels == [
        "Review the six demonstration work orders",
        "Verify the FortyGuard temperature evidence",
        "Compare planning methods",
        "How the score works—and what comes next",
    ]
    assert "Planning support, not safety clearance" in text
    assert "not a regulatory limit" in text
    assert "does not determine that work is safe" in text
    assert "modeled ambient-heat load" in text.lower()
    assert "degree-hours" in text.lower()
    assert not app.download_button
    assert not app.get("link_button")
    assert rendered_states.network_calls == []
