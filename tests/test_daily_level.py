"""Tests for reading the one same-day signal the API returns."""

from datetime import UTC, date, datetime, timedelta

import pytest

from certiroute.collection import HeatmapSnapshotStore
from certiroute.daily_level import (
    build_daily_level_request,
    collect_daily_level,
)
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard.geometry import bounding_polygon

TODAY = date(2026, 8, 22)
NOW = datetime(2026, 8, 22, 15, 0, tzinfo=UTC)


def job(job_id: str, *, longitude: float, latitude: float) -> Job:
    return Job(
        job_id=job_id,
        name=job_id,
        location=GeoPoint(longitude=longitude, latitude=latitude),
        duration_minutes=30,
    )


JOBS = [
    job("A", longitude=-112.07, latitude=33.44),
    job("B", longitude=-112.05, latitude=33.46),
]
POLYGON = bounding_polygon(item.location for item in JOBS)


def result(temperature_c: float) -> dict:
    """One tile large enough to cover every job in this fixture."""

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


class FakeClient:
    def __init__(self, temperature_c: float = 38.5) -> None:
        self.temperature_c = temperature_c
        self.submitted: list = []

    def submit_heatmap(self, request):
        self.submitted.append(request)
        return "activity-123"

    def wait_for_activity(self, activity_id, **_):
        return result(self.temperature_c)


def test_request_asks_for_the_whole_day_aggregate() -> None:
    request = build_daily_level_request(POLYGON, target_date=TODAY, granularity=60)

    assert request.date_time.filter_type == 3
    assert request.date_time.start_date == TODAY
    assert request.granularity == 60


def test_a_fetched_level_is_returned_for_every_site(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    client = FakeClient(38.5)

    reading = collect_daily_level(
        JOBS, POLYGON, store, target_date=TODAY, granularity=60,
        client=client, now_utc=NOW,
    )

    assert set(reading.level_by_job) == {"A", "B"}
    assert reading.level_by_job["A"] == pytest.approx(38.5)
    assert reading.area_mean_c == pytest.approx(38.5)
    assert not reading.cache_hit
    assert len(client.submitted) == 1


def test_todays_level_is_reused_only_while_it_is_fresh(tmp_path) -> None:
    """Today's aggregate keeps moving, so a stale copy must not be reused."""

    store = HeatmapSnapshotStore(tmp_path)
    client = FakeClient(38.5)
    collect_daily_level(
        JOBS, POLYGON, store, target_date=TODAY, granularity=60,
        client=client, now_utc=NOW,
    )

    fresh = collect_daily_level(
        JOBS, POLYGON, store, target_date=TODAY, granularity=60,
        client=client, now_utc=NOW + timedelta(minutes=5),
    )
    assert fresh.cache_hit
    assert len(client.submitted) == 1

    stale = collect_daily_level(
        JOBS, POLYGON, store, target_date=TODAY, granularity=60,
        client=client, now_utc=NOW + timedelta(hours=4),
    )
    assert not stale.cache_hit
    assert len(client.submitted) == 2


def test_a_finished_day_is_cached_forever(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    client = FakeClient(37.0)
    past = date(2026, 8, 10)

    collect_daily_level(
        JOBS, POLYGON, store, target_date=past, granularity=60,
        client=client, now_utc=NOW,
    )
    later = collect_daily_level(
        JOBS, POLYGON, store, target_date=past, granularity=60,
        client=client, now_utc=NOW + timedelta(days=30),
    )

    assert later.cache_hit
    assert len(client.submitted) == 1


def test_offline_use_without_a_cached_reading_is_refused(tmp_path) -> None:
    """A missing anchor must never be replaced with an invented level."""

    store = HeatmapSnapshotStore(tmp_path)

    with pytest.raises(LookupError, match="no cached whole-day aggregate"):
        collect_daily_level(
            JOBS, POLYGON, store, target_date=TODAY, granularity=60,
            client=None, now_utc=NOW,
        )


def test_a_cached_finished_day_is_readable_with_no_client(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    past = date(2026, 8, 10)
    collect_daily_level(
        JOBS, POLYGON, store, target_date=past, granularity=60,
        client=FakeClient(36.0), now_utc=NOW,
    )

    offline = collect_daily_level(
        JOBS, POLYGON, store, target_date=past, granularity=60,
        client=None, now_utc=NOW,
    )

    assert offline.cache_hit
    assert offline.level_by_job["B"] == pytest.approx(36.0)


def test_no_jobs_is_refused(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)

    with pytest.raises(ValueError, match="at least one job"):
        collect_daily_level(
            [], POLYGON, store, target_date=TODAY, client=FakeClient(), now_utc=NOW
        )
