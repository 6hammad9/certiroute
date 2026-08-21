from datetime import UTC, date, datetime, time

import pytest

from certiroute.collection import HeatmapSnapshotStore
from certiroute.domain import GeoPoint, Job
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    plan_profile_collection,
)


def _job(job_id: str, *, longitude: float, latitude: float) -> Job:
    return Job(
        job_id=job_id,
        name=job_id,
        location=GeoPoint(longitude=longitude, latitude=latitude),
        duration_minutes=30,
    )


def _result(temperature_c: float) -> dict:
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
    def __init__(self) -> None:
        self.requested_minutes: list[int] = []

    def create_heatmap(
        self,
        request,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ):
        del poll_interval_seconds, max_attempts
        start = request.date_time.start_time
        minute = start.hour * 60 + start.minute
        self.requested_minutes.append(minute)
        return f"activity-{minute}", _result(20 + start.hour)


def test_builds_one_shared_aoi_request_per_sample_time() -> None:
    jobs = [
        _job("A", longitude=-112.08, latitude=33.44),
        _job("B", longitude=-112.06, latitude=33.46),
    ]

    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
        granularity=80,
    )

    assert list(requests) == [480, 720, 1020]
    assert {request.granularity for request in requests.values()} == {80}
    assert (
        len({request.polygon_aoi.model_dump_json() for request in requests.values()})
        == 1
    )


def test_collection_requires_confirmed_task_cap_before_network(tmp_path) -> None:
    jobs = [_job("A", longitude=-112.08, latitude=33.44)]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
    )
    store = HeatmapSnapshotStore(tmp_path)
    client = FakeClient()

    with pytest.raises(ValueError, match="more new API tasks"):
        collect_real_temperature_batch(
            jobs,
            requests,
            store,
            client=client,
            max_new_tasks=2,
            now_utc=datetime(2026, 8, 21, tzinfo=UTC),
        )

    assert client.requested_minutes == []
    assert list(tmp_path.rglob("*.json")) == []


def test_collects_real_profiles_then_reuses_exact_historical_cache(tmp_path) -> None:
    jobs = [
        _job("A", longitude=-112.08, latitude=33.44),
        _job("B", longitude=-112.06, latitude=33.46),
    ]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
    )
    store = HeatmapSnapshotStore(tmp_path)
    client = FakeClient()
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)

    initial_plan = plan_profile_collection(requests, store, now_utc=now)
    assert initial_plan.cache_hit_count == 0
    assert initial_plan.new_task_count == 3

    first = collect_real_temperature_batch(
        jobs,
        requests,
        store,
        client=client,
        max_new_tasks=initial_plan.new_task_count,
        now_utc=now,
        clock=lambda: now,
    )

    assert client.requested_minutes == [480, 720, 1020]
    assert [point.temperature_c for point in first.profiles["A"].points] == [
        28,
        32,
        37,
    ]
    assert all(not sample.cache_hit for sample in first.samples)

    cached_plan = plan_profile_collection(requests, store, now_utc=now)
    assert cached_plan.cache_hit_count == 3
    assert cached_plan.new_task_count == 0
    second = collect_real_temperature_batch(
        jobs,
        requests,
        store,
        client=None,
        max_new_tasks=0,
        now_utc=now,
    )

    assert second.profiles == first.profiles
    assert all(sample.cache_hit for sample in second.samples)
    assert len(list(tmp_path.rglob("*.json"))) == 3


def test_profile_request_builder_rejects_pre_hackathon_data_floor() -> None:
    job = _job("A", longitude=-112.08, latitude=33.44)

    with pytest.raises(ValueError, match="2021-01-01"):
        build_profile_requests([job], target_date=date(2020, 12, 31))


def test_profile_request_builder_rejects_duplicate_jobs_before_api_planning() -> None:
    job = _job("A", longitude=-112.08, latitude=33.44)

    with pytest.raises(ValueError, match="job IDs must be unique"):
        build_profile_requests([job, job], target_date=date(2025, 7, 15))
