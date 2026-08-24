"""UI contract tests for the guided, real-data-only Streamlit experience."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import certiroute.map_picker as map_picker
from certiroute.collection import HeatmapSnapshotStore, SnapshotTemporalScope
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.map_scenario import MapScenarioState
from certiroute.real_conditions import build_profile_requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "main.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
REPLAY_DATE = date(2026, 7, 15)
# Reviewing a day collects back to the earliest candidate start, because
# grading has to see the hours it is asked to judge.
SAMPLE_TIMES = tuple(time(hour) for hour in range(5, 18))

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


@dataclass
class MapDriver:
    """Replace the Folium component with one controllable AppTest boundary."""

    last_clicked: dict[str, float] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def render(
        self,
        operating_area: object,
        *,
        depot: object,
        job_sites: object,
        coverage: object = None,
        generation: int = 0,
        height: int = 500,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append(
            {
                "operating_area": operating_area,
                "depot": depot,
                "job_sites": tuple(job_sites),  # type: ignore[arg-type]
                "coverage": coverage,
                "generation": generation,
                "height": height,
            }
        )
        return {"last_clicked": self.last_clicked}


@dataclass(frozen=True)
class RenderedStates:
    """Network-isolated onboarding, map, import, and result product states."""

    empty: AppTest
    duplicate: AppTest
    map_ready: AppTest
    import_waiting_for_depot: AppTest
    import_ready: AppTest
    infeasible_import: AppTest
    crew: AppTest
    planner: AppTest
    map_driver: MapDriver
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


def _run_app(driver: MapDriver) -> AppTest:
    driver.last_clicked = None
    app = AppTest.from_file(APP_PATH)
    app.run(timeout=60)
    assert not app.exception
    return app


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _click_map(
    app: AppTest,
    driver: MapDriver,
    *,
    latitude: float,
    longitude: float,
) -> AppTest:
    """Publish one click; its repeated rerun payload must be ignored by the app."""

    driver.last_clicked = {"lat": latitude, "lng": longitude}
    app.run(timeout=60)
    driver.last_clicked = None
    assert not app.exception
    return app


def _scenario(app: AppTest) -> MapScenarioState:
    value = app.session_state["certiroute_map_scenario"]
    if isinstance(value, MapScenarioState):
        return value
    return MapScenarioState.from_dict(value)


def _review_finished_day(app: AppTest, target: date = REPLAY_DATE) -> AppTest:
    """Switch from planning today to reviewing a day with measured evidence.

    The app opens on today because that is what a dispatcher needs. These
    contract tests cover the review path, which is the one backed by cached
    hourly snapshots rather than a live reading.
    """

    widget = next(
        item for item in app.date_input if str(item.key).startswith("workday_date_")
    )
    widget.set_value(target).run(timeout=60)
    assert not app.exception
    return app


def _import_jobs(app: AppTest, upload: tuple[str, bytes, str]) -> AppTest:
    app.file_uploader(key="job_manifest_upload").upload(*upload).run(timeout=60)
    assert not app.exception
    _button(app, "Use these imported jobs").click().run(timeout=60)
    assert not app.exception
    return app


def _load_example_and_build(app: AppTest) -> AppTest:
    """Load the secondary walkthrough and explicitly create its route."""

    _button(app, "Load the Phoenix walkthrough").click().run(timeout=60)
    assert not app.exception
    _review_finished_day(app)
    _button(app, "Create my heat-aware route").click().run(timeout=60)
    assert not app.exception
    return app


VALID_UPLOAD = (
    "real-work-orders.csv",
    (
        b"job_id,name,latitude,longitude,duration_minutes,priority,"
        b"earliest_start,latest_finish\n"
        b"REAL-001,Customer rooftop inspection,33.44855,-112.07391,45,5,"
        b"08:00,12:00\n"
        b"REAL-002,Customer cooling-unit service,33.44530,-112.06670,60,3,"
        b"09:00,16:00\n"
    ),
    "text/csv",
)

INFEASIBLE_UPLOAD = (
    "infeasible-work-orders.csv",
    (
        b"job_id,name,latitude,longitude,duration_minutes,priority,"
        b"earliest_start,latest_finish\n"
        b"TIGHT-001,First simultaneous job,33.44855,-112.07391,60,5,"
        b"08:00,09:00\n"
        b"TIGHT-002,Second simultaneous job,33.44860,-112.07385,60,5,"
        b"08:00,09:00\n"
    ),
    "text/csv",
)


@pytest.fixture(scope="module")
def rendered_states(tmp_path_factory: pytest.TempPathFactory) -> RenderedStates:
    """Render every state while making unexpected network use impossible."""

    patch = pytest.MonkeyPatch()
    driver = MapDriver()
    network_calls: list[str] = []

    def reject_network(*_args: object, **_kwargs: object) -> object:
        network_calls.append("create_heatmap")
        raise AssertionError("AppTest must not submit a FortyGuard request")

    patch.setenv("FORTYGUARD_API_KEY", "ui-test-key")
    patch.setattr(FortyGuardClient, "create_heatmap", reject_network)
    patch.setattr(map_picker, "render_map_picker", driver.render)
    get_settings.cache_clear()

    empty_root = tmp_path_factory.mktemp("empty_heatmap_cache")
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(empty_root))
    empty = _run_app(driver)

    duplicate = _run_app(driver)
    _click_map(duplicate, driver, latitude=33.44855, longitude=-112.07391)
    _click_map(duplicate, driver, latitude=33.44530, longitude=-112.06670)
    _click_map(duplicate, driver, latitude=33.44530, longitude=-112.06670)

    map_ready = _run_app(driver)
    _click_map(map_ready, driver, latitude=33.44855, longitude=-112.07391)
    _click_map(map_ready, driver, latitude=33.44530, longitude=-112.06670)
    _click_map(map_ready, driver, latitude=33.45210, longitude=-112.07830)

    import_waiting_for_depot = _import_jobs(_run_app(driver), VALID_UPLOAD)

    import_ready = _import_jobs(_run_app(driver), VALID_UPLOAD)
    _click_map(import_ready, driver, latitude=33.44855, longitude=-112.07391)

    infeasible_import = _import_jobs(_run_app(driver), INFEASIBLE_UPLOAD)
    _click_map(infeasible_import, driver, latitude=33.44855, longitude=-112.07391)
    _review_finished_day(infeasible_import)
    _button(infeasible_import, "Create my heat-aware route").click().run(timeout=60)
    assert not infeasible_import.exception

    cached_root = tmp_path_factory.mktemp("cached_heatmap_cache")
    _publish_cached_replay(cached_root)
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(cached_root))
    crew = _load_example_and_build(_run_app(driver))

    # Keep planner assertions independent from the crew-facing session.
    planner = _load_example_and_build(_run_app(driver))
    planner.segmented_control(key="view_mode").select("Planner details").run(timeout=60)
    assert not planner.exception

    try:
        yield RenderedStates(
            empty=empty,
            duplicate=duplicate,
            map_ready=map_ready,
            import_waiting_for_depot=import_waiting_for_depot,
            import_ready=import_ready,
            infeasible_import=infeasible_import,
            crew=crew,
            planner=planner,
            map_driver=driver,
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


def test_first_run_is_guided_map_first_and_makes_no_network_request(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.empty
    text = _all_text(app)

    # The product name is a wordmark; the headline is the page's only h1, so
    # nothing competes with it to be read first.
    assert '<div class="wordmark">' in text
    assert "CertiRoute" in text
    assert not app.title
    assert '<h1 class="hero-heading">' in text
    assert "Start the shift before the heat does" in text
    # The hero states the premise before the promise.
    assert "work the hottest hours of the day by default" in text
    assert '<div class="hero-band">' in text
    assert all(color in text for color in ("#70FFD2", "#FFFC8C", "#FFCC4D", "#FF9137"))
    assert "color-scheme: light" in text
    # The chosen palette must survive restyling, and type must be explicit.
    assert "--canvas: #F7F8FA" in text
    assert "Instrument Sans" in text
    assert "JetBrains Mono" in text
    # The guide reports progress, so on a first run every step is still ahead.
    assert "Place the crew base" in text
    assert "Add work sites" in text
    assert "Plan the shift" in text
    assert 'class="process-step active"' in text
    assert "First, click where the crew starts and returns" in text
    assert app.selectbox[0].label == (
        "Start near a U.S. city — pan anywhere in the U.S."
    )
    assert app.selectbox[0].value == "Phoenix, Arizona"

    assert not app.date_input
    assert not app.time_input
    assert not app.number_input
    assert not app.metric
    assert not app.dataframe
    assert "latitude" not in text.lower()
    assert "longitude" not in text.lower()
    assert [expander.label for expander in app.expander] == [
        "Advanced: import work orders or load the walkthrough"
    ]
    assert app.file_uploader[0].label == "Import work orders (CSV)"
    assert _button(app, "Undo last point").disabled
    assert _button(app, "Start over").disabled
    assert rendered_states.map_driver.calls
    assert rendered_states.network_calls == []


def test_repeated_component_payload_does_not_duplicate_a_work_site(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.duplicate
    state = _scenario(app)
    text = _all_text(app)

    assert state.depot is not None
    assert state.job_count == 1
    assert "Add 1 more orange work site" in text
    assert "Ready — 2 work sites selected" not in text
    assert not any(
        button.label in {"Plan today's shift", "Create my heat-aware route"}
        for button in app.button
    )
    assert rendered_states.network_calls == []


def test_map_clicks_create_a_ready_route_setup_with_plain_defaults(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.map_ready
    state = _scenario(app)
    text = _all_text(app)

    assert state.depot is not None
    assert state.job_count == 2
    assert "Ready — 2 work sites selected" in text
    assert "Crew start &amp; return" in text
    assert "Work site 1" in text
    assert "Work site 2" in text
    assert text.count("45 min on site") >= 2
    # The app opens ready to plan the day the crew is actually working.
    assert "Planning today" in text
    assert _button(app, "Plan today's shift").disabled is False
    assert {widget.label for widget in app.number_input} == {
        "Minutes at job 1",
        "Minutes at job 2",
    }
    assert {widget.label for widget in app.time_input} == {
        "Crew normally starts",
        "Crew finishes",
    }
    assert "latitude" not in text.lower()
    assert "longitude" not in text.lower()
    assert rendered_states.network_calls == []


def test_csv_import_is_optional_and_only_asks_for_one_map_click(
    rendered_states: RenderedStates,
) -> None:
    waiting = rendered_states.import_waiting_for_depot
    ready = rendered_states.import_ready
    waiting_text = _all_text(waiting)
    ready_text = _all_text(ready)

    waiting_state = _scenario(waiting)
    assert waiting_state.depot is None
    assert waiting_state.job_count == 2
    assert "Your 2 imported work sites are already orange" in waiting_text
    assert "One map click places the mint crew base" in waiting_text
    assert not any(
        button.label in {"Plan today's shift", "Create my heat-aware route"}
        for button in waiting.button
    )

    ready_state = _scenario(ready)
    assert ready_state.depot is not None
    assert ready_state.job_count == 2
    assert "Ready — 2 work sites selected" in ready_text
    assert _button(ready, "Plan today's shift").disabled is False
    assert not ready.text_input
    assert not ready.number_input
    assert "Depot latitude" not in ready_text
    assert "Depot longitude" not in ready_text
    assert rendered_states.network_calls == []


def test_infeasible_import_is_blocked_before_any_temperature_request(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.infeasible_import
    text = _all_text(app)

    assert "No complete depot-to-depot route fits" in text
    assert "No FortyGuard request was submitted" in text
    assert rendered_states.network_calls == []


def test_crew_route_leads_with_one_plain_language_decision(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.crew
    text = _all_text(app)
    control = app.segmented_control(key="view_mode")

    assert control.options == ["Crew route", "Planner details"]
    assert control.value == "Crew route"
    assert "Reviewing a finished day" in text
    assert (
        "Reordering would not have helped" in text
        or "The order that ran coolest" in text
    )
    assert "First stop" in text
    assert "Jobs on time" in text
    assert "Follow this route" in text
    assert "Stop 1 is where the shift begins" in text
    assert "Return to crew base" in text
    decision_markup = next(
        str(element.value)
        for element in app.markdown
        if 'class="bento-hero decision-card"' in str(element.value)
    )
    assert 'class="bento-tile route-fact stop"' in decision_markup
    assert 'class="bento-tile route-fact time"' in decision_markup
    assert 'class="bento-tile route-fact status"' in decision_markup
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
    assert "Return to crew base" in route_markup
    assert "\n" not in route_markup


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
    assert [112, 255, 210, 255] in [
        layer["getColor"] for layer in layers if layer["@@type"] == "PathLayer"
    ]

    stop_layer = next(
        layer
        for layer in layers
        if layer["@@type"] == "ScatterplotLayer" and layer.get("getRadius") == 17
    )
    assert stop_layer["getFillColor"] == [255, 145, 55, 255]

    route_layer = next(layer for layer in layers if layer["@@type"] == "PathLayer")
    assert len(route_layer["data"][0]["path"]) == 8

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

    downloads = [
        button
        for button in app.download_button
        if button.label == "Download crew route (CSV)"
    ]
    assert len(downloads) == 1
    assert downloads[0].proto.url


def test_crew_route_hides_planner_complexity(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.crew
    text = _all_text(app)

    assert not app.metric
    assert all("Plan" not in frame.value.columns for frame in app.dataframe)
    assert not app.get("vega_lite_chart")
    assert "Modeled exposure" not in text
    assert "degree-hours" not in text
    assert "FortyGuard activity ID" not in text
    assert "Compare planning methods" not in text
    assert "Planning aid—not safety clearance" in text


def test_planner_view_owns_comparison_metrics_and_method_table(
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
    assert "13 heatmaps across 1 area" in text
    assert "60 m" in text
    assert "No synthetic or substitute temperature profile" in text
    assert len(temperatures) == 6
    assert list(temperatures.columns[1:]) == [f"{hour:02d}:00" for hour in range(5, 18)]
    assert len(source_records) == 13
    assert (source_records["Heat-data area"] == 1).all()
    assert (source_records["Retrieved"] == "Saved API response").all()
    assert (
        source_records["FortyGuard activity ID"]
        .str.startswith("fixture-activity-")
        .all()
    )


def test_planner_view_keeps_methods_and_safety_detail_secondary(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.planner
    text = _all_text(app)
    expander_labels = [expander.label for expander in app.expander]

    assert "Verify the FortyGuard temperature evidence" in expander_labels
    assert "Compare planning methods" in expander_labels
    assert "How the score works—and what comes next" in expander_labels
    assert "Planning support, not safety clearance" in text
    assert "not a regulatory limit" in text
    assert "does not determine that work is safe" in text
    assert "modeled ambient-heat load" in text.lower()
    assert "degree-hours" in text.lower()
    assert not [
        button
        for button in app.download_button
        if button.label == "Download crew route (CSV)"
    ]
    assert not app.get("link_button")
    assert rendered_states.network_calls == []


# --- The guide, and the limit it points at ----------------------------------


def test_the_guide_marks_finished_steps_and_points_at_the_next(
    rendered_states: RenderedStates,
) -> None:
    """A static strip says what the product does, never what to do now."""

    first_run = _all_text(rendered_states.empty)
    with_sites = _all_text(rendered_states.map_ready)

    # Nothing done yet: placing the base is the active step.
    assert first_run.count('class="process-step done"') == 0
    assert 'class="process-step active"' in first_run

    # Base placed and sites added: the first two steps are finished.
    assert with_sites.count('class="process-step done"') == 2
    assert 'class="process-step active"' in with_sites


def test_every_guide_step_carries_a_hover_explanation(
    rendered_states: RenderedStates,
) -> None:
    text = _all_text(rendered_states.empty)

    assert 'title="Click once on the map.' in text
    assert 'title="Click each place the crew must visit' in text
    assert 'title="CertiRoute reads today&#x27;s heat' in text


def test_the_map_is_told_where_the_trained_model_applies(
    rendered_states: RenderedStates,
) -> None:
    """The boundary is drawn rather than enforced only after setup."""

    coverage = rendered_states.map_driver.calls[-1]["coverage"]

    assert coverage is not None
    assert coverage.radius_km == pytest.approx(60.0)
    assert "Phoenix" in coverage.label
