"""UI contract tests for reviewing and grading a finished day.

Review mode exists to answer one question honestly: on a day the model never
saw, would its recommendation have helped? These tests drive that path with
every network route closed, so the grading runs entirely on cached evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytest
from streamlit.testing.v1 import AppTest

import certiroute.map_picker as map_picker
from certiroute.climatology import (
    ClimatologyEvaluation,
    DiurnalClimatology,
    save_climatology,
)
from certiroute.collection import HeatmapSnapshotStore, SnapshotTemporalScope
from certiroute.config import get_settings
from certiroute.daily_level import build_daily_level_request
from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import DailyLevelShape
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.real_conditions import build_profile_requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "main.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
GRANULARITY = 60

# A finished day the fixture model never trained or calibrated on.
CLEAN_DAY = date(2026, 6, 10)
TRAINING_DAY = date(2026, 6, 1)
CALIBRATION_DAY = date(2026, 6, 5)

HOURS = range(5, 18)
DAY_LEVEL_C = 36.0
# The fixture day warms steadily, so an earlier start is genuinely cooler and
# the grading has something real to detect.
BASE_C = 26.0
RISE_C = 1.4


@dataclass
class MapDriver:
    last_clicked: dict[str, float] | None = None
    calls: list[dict[str, object]] = field(default_factory=list)

    def render(
        self,
        operating_area: object,
        *,
        depot: object,
        job_sites: object,
        generation: int = 0,
        height: int = 500,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append({"generation": generation})
        return {"last_clicked": self.last_clicked}


def _jobs() -> list[Job]:
    frame = pd.read_csv(SAMPLE_PATH)
    return [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
        )
        for row in frame.itertuples(index=False)
    ]


def _model() -> DiurnalClimatology:
    """Offsets that match the fixture day exactly, so grading is deterministic."""

    offsets = {hour * 60: BASE_C + RISE_C * (hour - 5) - DAY_LEVEL_C for hour in HOURS}
    return DiurnalClimatology(
        area_id="phoenix",
        label="Phoenix, Arizona",
        granularity_m=GRANULARITY,
        shape=DailyLevelShape(
            offsets_by_minute=offsets,
            sample_counts=dict.fromkeys(offsets, 48),
            day_count=8,
        ),
        training_dates=(TRAINING_DAY, date(2026, 6, 2)),
        evaluation=ClimatologyEvaluation(
            holdout_dates=(CALIBRATION_DAY,),
            mean_absolute_error_c=0.55,
            worst_absolute_error_c=1.10,
            reading_count=156,
            day_scores_c=(0.7, 0.9, 1.1),
            unseen_site_mae_c=0.58,
        ),
        trained_at_utc=datetime(2026, 6, 6, tzinfo=UTC),
    )


def _tile_result(jobs: list[Job], temperature_c: float) -> dict[str, Any]:
    half = 0.0002
    return {
        "map_data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "tile_id": f"fixture-{job.job_id}",
                        "average_temperature": temperature_c,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [
                                    job.location.longitude - half,
                                    job.location.latitude - half,
                                ],
                                [
                                    job.location.longitude + half,
                                    job.location.latitude - half,
                                ],
                                [
                                    job.location.longitude + half,
                                    job.location.latitude + half,
                                ],
                                [
                                    job.location.longitude - half,
                                    job.location.latitude + half,
                                ],
                                [
                                    job.location.longitude - half,
                                    job.location.latitude - half,
                                ],
                            ]
                        ],
                    },
                }
                for job in jobs
            ],
        }
    }


def _publish_day(cache_root: Path, target: date = CLEAN_DAY) -> None:
    """Cache both things grading needs: the hourly truth and the day's anchor."""

    jobs = _jobs()
    store = HeatmapSnapshotStore(cache_root)
    collected = datetime(2026, 6, 11, 12, tzinfo=UTC)

    hourly = build_profile_requests(
        jobs,
        target_date=target,
        sample_times=tuple(time(hour) for hour in HOURS),
        granularity=GRANULARITY,
    )
    for minute, request in hourly.items():
        store.publish(
            request,
            activity_id=f"fixture-hour-{target:%m%d}-{minute}",
            collected_at_utc=collected,
            temporal_scope=SnapshotTemporalScope.HISTORICAL,
            raw_result=_tile_result(jobs, BASE_C + RISE_C * (minute // 60 - 5)),
        )

    store.publish(
        build_daily_level_request(
            bounding_polygon(job.location for job in jobs),
            target_date=target,
            granularity=GRANULARITY,
        ),
        activity_id=f"fixture-aggregate-{target:%m%d}",
        collected_at_utc=collected,
        temporal_scope=SnapshotTemporalScope.HISTORICAL,
        raw_result=_tile_result(jobs, DAY_LEVEL_C),
    )


def _all_text(app: AppTest) -> str:
    parts: list[str] = []
    for group in (
        app.markdown,
        app.caption,
        app.warning,
        app.error,
        app.info,
        app.success,
    ):
        parts += [str(element.value) for element in group]
    return "\n".join(parts)


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


def _review(app: AppTest, target: date) -> AppTest:
    widget = next(
        item for item in app.date_input if str(item.key).startswith("workday_date_")
    )
    widget.set_value(target).run(timeout=60)
    assert not app.exception
    return app


@pytest.fixture(scope="module")
def review_states(tmp_path_factory: pytest.TempPathFactory):
    patch = pytest.MonkeyPatch()
    driver = MapDriver()
    network: list[str] = []

    def reject(*_args: object, **_kwargs: object) -> object:
        network.append("network")
        raise AssertionError("AppTest must not submit a FortyGuard request")

    patch.setenv("FORTYGUARD_API_KEY", "ui-test-key")
    patch.setattr(FortyGuardClient, "create_heatmap", reject)
    patch.setattr(FortyGuardClient, "submit_heatmap", reject)
    patch.setattr(map_picker, "render_map_picker", driver.render)
    get_settings.cache_clear()

    cache_root = tmp_path_factory.mktemp("review_cache")
    _publish_day(cache_root, CLEAN_DAY)
    # A training day needs measurements too, so the refusal is reached through
    # the real path rather than by the route simply failing to build.
    _publish_day(cache_root, TRAINING_DAY)
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(cache_root))

    model_root = tmp_path_factory.mktemp("review_climatology")
    save_climatology(_model(), root=model_root)
    patch.setenv("CERTIROUTE_CLIMATOLOGY_PATH", str(model_root))

    def build(target: date) -> AppTest:
        app = AppTest.from_file(APP_PATH)
        app.run(timeout=60)
        _button(app, "Load the Phoenix walkthrough").click().run(timeout=60)
        _review(app, target)
        _button(app, "Create my heat-aware route").click().run(timeout=60)
        assert not app.exception
        return app

    clean = build(CLEAN_DAY)
    graded = build(CLEAN_DAY)
    _button(graded, "Grade this day").click().run(timeout=60)
    assert not graded.exception

    training = build(TRAINING_DAY)

    try:
        yield {
            "clean": clean,
            "graded": graded,
            "training": training,
            "network": network,
        }
    finally:
        patch.undo()
        get_settings.cache_clear()


def test_review_mode_offers_to_grade_a_clean_day(review_states) -> None:
    app = review_states["clean"]
    text = _all_text(app)

    assert "Reviewing a finished day" in text
    assert "Did the recommendation hold up?" in text
    assert _button(app, "Grade this day") is not None
    assert review_states["network"] == []


def test_grading_reports_the_choice_and_the_hindsight_best(review_states) -> None:
    app = review_states["graded"]
    metrics = {metric.label: metric.value for metric in app.metric}

    assert metrics["It recommended"] == "05:00"
    assert metrics["Best in hindsight"] == "05:00"
    assert metrics["Regret"] == "0.0"
    assert metrics["Exposure actually avoided"].endswith("%")


def test_grading_states_plainly_when_the_model_was_right(review_states) -> None:
    text = _all_text(review_states["graded"])

    assert "picked the best available start" in text
    assert "without seeing a single hourly temperature" in text


def test_a_training_day_is_refused_rather_than_graded(review_states) -> None:
    """Grading a day the model learned from would measure memory, not skill."""

    app = review_states["training"]
    text = _all_text(app)

    assert "would measure memory rather than skill" in text
    assert not any(button.label == "Grade this day" for button in app.button)


def test_grading_never_touched_the_network(review_states) -> None:
    assert review_states["network"] == []


# --- Dates the vendor has not published yet ---------------------------------


def test_a_day_inside_the_publishing_lag_is_refused_before_any_request(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """Offering a date that can only fail wastes the operator's wait.

    FortyGuard publishes hourly history about two days behind, so yesterday
    returns zero tiles for every hour. The app used to discover that only after
    the request had been sent and waited on.
    """

    patch = pytest.MonkeyPatch()
    driver = MapDriver()
    network: list[str] = []

    def reject(*_args: object, **_kwargs: object) -> object:
        network.append("network")
        raise AssertionError("no request may be sent for an unpublished day")

    patch.setenv("FORTYGUARD_API_KEY", "ui-test-key")
    patch.setattr(FortyGuardClient, "create_heatmap", reject)
    patch.setattr(FortyGuardClient, "submit_heatmap", reject)
    patch.setattr(map_picker, "render_map_picker", driver.render)
    get_settings.cache_clear()
    patch.setenv(
        "CERTIROUTE_HEATMAP_CACHE_PATH", str(tmp_path_factory.mktemp("lag_cache"))
    )
    model_root = tmp_path_factory.mktemp("lag_climatology")
    save_climatology(_model(), root=model_root)
    patch.setenv("CERTIROUTE_CLIMATOLOGY_PATH", str(model_root))

    try:
        app = AppTest.from_file(APP_PATH)
        app.run(timeout=60)
        _button(app, "Load the Phoenix walkthrough").click().run(timeout=60)
        _review(app, date.today() - timedelta(days=1))
        text = _all_text(app)

        assert "has not published hourly temperatures" in text
        assert "runs about 2 days behind" in text
        assert not any(
            button.label == "Create my heat-aware route" for button in app.button
        )
        assert network == []
    finally:
        patch.undo()
        get_settings.cache_clear()
