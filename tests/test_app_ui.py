"""UI contract tests for the real-data-only Streamlit experience."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any

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
    """The two network-isolated UI states the product must support."""

    empty: AppTest
    cached: AppTest
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
    cached = _run_app()

    try:
        yield RenderedStates(
            empty=empty,
            cached=cached,
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
    assert "Put outdoor work in cooler feasible hours" in text
    assert "Review the work" in text
    assert "Load the heat" in text
    assert "Read the trade-off" in text
    assert "REAL FORTYGUARD TEMPERATURES" in text
    assert "Your recommendation will appear here" in text

    assert len(app.button) == 1
    assert app.button[0].label == "Build heat-aware schedule"
    assert not app.radio
    assert not app.checkbox
    assert not app.metric
    assert not app.dataframe
    assert "synthetic fallback" not in text.lower()
    assert rendered_states.network_calls == []


def test_cached_real_replay_states_a_plain_language_decision(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.cached
    decision_text = " ".join(
        str(element.value) for element in [*app.success, *app.info]
    )

    assert (
        "Start " in decision_text
        or "Use the heat-aware order" in decision_text
        or "Keep the distance-efficient route" in decision_text
    )
    assert "valid planning result, not a data failure" in decision_text or app.success
    assert rendered_states.network_calls == []


def test_cached_real_replay_has_four_dispatcher_metrics(
    rendered_states: RenderedStates,
) -> None:
    metrics = {metric.label: metric.value for metric in rendered_states.cached.metric}

    assert set(metrics) == {
        "Modeled exposure",
        "Hot-work time ≥35 °C",
        "Added estimated travel",
        "Jobs completed on time",
    }
    assert metrics["Jobs completed on time"] == "6 / 6"


def test_cached_real_replay_draws_only_the_two_customer_plans(
    rendered_states: RenderedStates,
) -> None:
    charts = rendered_states.cached.get("vega_lite_chart")

    assert len(charts) == 1
    chart_spec = json.loads(charts[0].proto.spec)
    serialized_spec = json.dumps(chart_spec)
    assert "Operations baseline" in serialized_spec
    assert "Heat-aware plan" in serialized_spec
    assert "Certainty-aware" not in serialized_spec

    methods = _dataframe_with_column(rendered_states.cached, "Plan")
    assert list(methods["Plan"]) == [
        "Distance-efficient operations baseline",
        "Heat-aware recommendation",
    ]


def test_cached_real_replay_provides_a_dispatcher_sequence(
    rendered_states: RenderedStates,
) -> None:
    sequence = _dataframe_with_column(rendered_states.cached, "Stop")

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


def test_cached_real_replay_exposes_auditable_api_evidence(
    rendered_states: RenderedStates,
) -> None:
    app = rendered_states.cached
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


def test_page_keeps_the_safety_boundary_visible(
    rendered_states: RenderedStates,
) -> None:
    text = _all_text(rendered_states.cached)

    assert "Planning support, not safety clearance" in text
    assert "not a regulatory limit" in text
    assert "does not determine that work is safe" in text
    assert "modeled ambient-heat load" in text.lower()
    assert "degree-hours" in text.lower()
