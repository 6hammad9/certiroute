"""UI contract tests for planning the day that is actually happening.

These cover the default path: the app opens on today, reads one whole-day
aggregate, and recommends a shift start. Network use is made impossible, and
the reading the app needs is pre-published to the cache, which also proves the
app reuses stored evidence instead of re-fetching it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_PATH = PROJECT_ROOT / "app" / "main.py"
SAMPLE_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
GRANULARITY = 60
TODAY_LEVEL_C = 38.0


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
        generation: int = 0,
        height: int = 500,
        **_kwargs: object,
    ) -> dict[str, object]:
        self.calls.append({"generation": generation, "height": height})
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
    """A Phoenix model whose mornings are genuinely cooler than its afternoons."""

    offsets = {hour * 60: 1.5 * (hour - 12) for hour in range(5, 18)}
    return DiurnalClimatology(
        area_id="phoenix",
        label="Phoenix, Arizona",
        granularity_m=GRANULARITY,
        shape=DailyLevelShape(
            offsets_by_minute=offsets,
            sample_counts=dict.fromkeys(offsets, 66),
            day_count=11,
        ),
        training_dates=tuple(date(2026, 8, day) for day in range(9, 20)),
        evaluation=ClimatologyEvaluation(
            holdout_dates=(date(2026, 8, 20), date(2026, 8, 21), date(2026, 8, 22)),
            mean_absolute_error_c=0.61,
            worst_absolute_error_c=1.44,
            reading_count=234,
            day_scores_c=(0.92, 1.18, 1.44),
            unseen_site_mae_c=0.63,
        ),
        trained_at_utc=datetime(2026, 8, 22, 12, tzinfo=UTC),
    )


def _tile(temperature_c: float) -> dict:
    """One tile large enough to cover every Phoenix demonstration site."""

    return {
        "map_data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "tile_id": "covering-tile",
                        "average_temperature": temperature_c,
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-113.0, 33.0],
                                [-111.0, 33.0],
                                [-111.0, 34.0],
                                [-113.0, 34.0],
                                [-113.0, 33.0],
                            ]
                        ],
                    },
                }
            ],
        }
    }


def _publish_todays_reading(root: Path) -> None:
    """Pre-cache the one same-day reading the app would otherwise fetch."""

    jobs = _jobs()
    request = build_daily_level_request(
        bounding_polygon(job.location for job in jobs),
        target_date=date.today(),
        granularity=GRANULARITY,
    )
    HeatmapSnapshotStore(root).publish(
        request,
        raw_result=_tile(TODAY_LEVEL_C),
        activity_id="cached-today-aggregate",
        temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST,
        collected_at_utc=datetime.now(UTC) - timedelta(minutes=1),
    )


def _all_text(app: AppTest) -> str:
    parts = [element.value for element in app.markdown]
    parts += [element.value for element in app.caption]
    parts += [str(element.value) for element in app.warning]
    parts += [str(element.value) for element in app.error]
    parts += [str(element.value) for element in app.info]
    parts += [str(element.value) for element in app.success]
    return "\n".join(str(part) for part in parts)


def _button(app: AppTest, label: str):
    return next(button for button in app.button if button.label == label)


@pytest.fixture(scope="module")
def today_states(tmp_path_factory: pytest.TempPathFactory):
    """Render the same-day path with every network route closed off."""

    patch = pytest.MonkeyPatch()
    driver = MapDriver()
    network_calls: list[str] = []

    def reject(*_args: object, **_kwargs: object) -> object:
        network_calls.append("network")
        raise AssertionError("AppTest must not submit a FortyGuard request")

    patch.setenv("FORTYGUARD_API_KEY", "ui-test-key")
    patch.setattr(FortyGuardClient, "create_heatmap", reject)
    patch.setattr(FortyGuardClient, "submit_heatmap", reject)
    patch.setattr(map_picker, "render_map_picker", driver.render)
    get_settings.cache_clear()

    cache_root = tmp_path_factory.mktemp("today_cache")
    _publish_todays_reading(cache_root)
    patch.setenv("CERTIROUTE_HEATMAP_CACHE_PATH", str(cache_root))

    model_root = tmp_path_factory.mktemp("climatology")
    save_climatology(_model(), root=model_root)
    patch.setenv("CERTIROUTE_CLIMATOLOGY_PATH", str(model_root))

    app = AppTest.from_file(APP_PATH)
    app.run(timeout=60)
    assert not app.exception
    _button(app, "Load the Phoenix walkthrough").click().run(timeout=60)
    assert not app.exception
    ready = app

    planned = AppTest.from_file(APP_PATH)
    planned.run(timeout=60)
    _button(planned, "Load the Phoenix walkthrough").click().run(timeout=60)
    _button(planned, "Plan today's shift").click().run(timeout=60)
    assert not planned.exception

    detail = AppTest.from_file(APP_PATH)
    detail.run(timeout=60)
    _button(detail, "Load the Phoenix walkthrough").click().run(timeout=60)
    _button(detail, "Plan today's shift").click().run(timeout=60)
    detail.segmented_control(key="view_mode").select("Planner details").run(timeout=60)
    assert not detail.exception

    # An area with no committed model must refuse rather than extrapolate.
    empty_model_root = tmp_path_factory.mktemp("no_climatology")
    patch.setenv("CERTIROUTE_CLIMATOLOGY_PATH", str(empty_model_root))
    untrained = AppTest.from_file(APP_PATH)
    untrained.run(timeout=60)
    _button(untrained, "Load the Phoenix walkthrough").click().run(timeout=60)
    assert not untrained.exception

    try:
        yield {
            "ready": ready,
            "planned": planned,
            "detail": detail,
            "untrained": untrained,
            "network_calls": network_calls,
        }
    finally:
        patch.undo()
        get_settings.cache_clear()


def test_the_app_opens_ready_to_plan_today(today_states) -> None:
    app = today_states["ready"]
    text = _all_text(app)

    assert "Planning today" in text
    assert "Plan today's shift" in text
    assert _button(app, "Plan today's shift").disabled is False
    assert today_states["network_calls"] == []


def test_the_headline_is_a_start_time_not_a_stop_order(today_states) -> None:
    """The decision this product makes is when to work, not what order."""

    text = _all_text(today_states["planned"])

    assert "Move the shift" in text
    assert "Start at 05:00" in text
    assert "cuts modelled heat exposure by" in text


def test_the_crew_view_states_the_interval_it_planned_against(today_states) -> None:
    text = _all_text(today_states["planned"])

    assert "Predicted within" in text
    # Three held-out day scores support 75% coverage and no more.
    assert "75%" in text
    assert "1.4 °C" in text


def test_every_candidate_start_is_shown_with_the_chosen_one_marked(
    today_states,
) -> None:
    text = _all_text(today_states["planned"])

    for label in ("05:00", "06:00", "07:00", "08:00"):
        assert f'<div class="timing-label">{label}</div>' in text
    # Each option is labelled with the heat it avoids against the usual start,
    # which is the quantity the decision turns on.
    assert "cooler" in text
    assert "your usual" in text
    assert 'class="timing-row picked"' in text


def test_the_crew_view_keeps_model_detail_out_of_the_way(today_states) -> None:
    text = _all_text(today_states["planned"])

    assert "Follow this route" in text
    assert "Held-out error" not in text
    assert "Error at unseen sites" not in text


def test_planner_details_expose_what_the_prediction_rests_on(today_states) -> None:
    app = today_states["detail"]
    text = _all_text(app)
    metrics = {metric.label for metric in app.metric}

    assert "What this prediction is built on" in text
    assert metrics >= {
        "Today's measured level",
        "Held-out error",
        "Error at unseen sites",
        "Trained on",
    }
    assert "split-conformal quantile" in text


def test_planner_details_report_the_ordering_result_honestly(today_states) -> None:
    """A null result is stated as a finding, not hidden."""

    text = _all_text(today_states["detail"])

    assert "Does the visit order matter today?" in text
    assert "when" in text and "order" in text


def test_planner_details_show_upper_bound_temperatures(today_states) -> None:
    app = today_states["detail"]
    text = _all_text(app)

    assert "Predicted conditions at each stop" in text
    assert "upper-bound temperatures" in text
    frame = app.dataframe[0].value
    assert "Planned-against temperature" in frame.columns
    assert len(frame) == 6


def test_an_untrained_area_refuses_to_plan_today(today_states) -> None:
    app = today_states["untrained"]
    text = _all_text(app)

    assert "no trained heat model" in text
    assert "pick a past date" in text
    assert not any(button.label == "Plan today's shift" for button in app.button)
    assert today_states["network_calls"] == []


def test_planning_today_never_touched_the_network(today_states) -> None:
    """The cached same-day reading is reused rather than re-fetched."""

    assert today_states["network_calls"] == []
