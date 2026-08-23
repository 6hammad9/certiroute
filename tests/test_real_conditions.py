import json
from datetime import UTC, date, datetime, time

import pytest

from certiroute.collection import CacheCorruptionError, HeatmapSnapshotStore
from certiroute.domain import GeoPoint, Job
from certiroute.real_conditions import (
    build_profile_requests,
    collect_real_temperature_batch,
    collect_real_temperature_batch_from_plan,
    plan_profile_collection,
)


def _main_snapshot_files(root) -> tuple:
    return tuple(
        path for path in root.rglob("*.json") if ".request_index" not in path.parts
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
    assert _main_snapshot_files(tmp_path) == ()


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
    assert len(_main_snapshot_files(tmp_path)) == 3


def test_planning_many_requests_scans_each_snapshot_only_once(
    tmp_path, monkeypatch
) -> None:
    jobs = [_job("A", longitude=-112.08, latitude=33.44)]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
    )
    store = HeatmapSnapshotStore(tmp_path)
    collected_at = datetime(2026, 8, 21, 12, tzinfo=UTC)
    for minute, request in requests.items():
        store.publish(
            request,
            activity_id=f"activity-{minute}",
            collected_at_utc=collected_at,
            temporal_scope="historical",
            raw_result=_result(20 + minute / 60),
        )

    original_get = store._get_indexed_snapshot
    loaded_ids: list[str] = []

    def tracked_get(pointer):
        loaded_ids.append(pointer.snapshot_id)
        return original_get(pointer)

    monkeypatch.setattr(store, "_get_indexed_snapshot", tracked_get)
    plan = plan_profile_collection(
        requests,
        store,
        now_utc=collected_at,
    )

    assert plan.cache_hit_count == 3
    assert len(loaded_ids) == 3
    assert len(set(loaded_ids)) == 3


def test_precomputed_fully_cached_plan_reuses_same_store_validated_snapshots(
    tmp_path, monkeypatch
) -> None:
    jobs = [_job("A", longitude=-112.08, latitude=33.44)]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    client = FakeClient()
    first = collect_real_temperature_batch(
        jobs,
        requests,
        store,
        client=client,
        max_new_tasks=3,
        now_utc=now,
        clock=lambda: now,
    )
    plan = plan_profile_collection(requests, store, now_utc=now)

    def unexpected_disk_read(*args, **kwargs):
        del args, kwargs
        raise AssertionError("a same-store validated plan must not reread disk")

    monkeypatch.setattr(store, "list_for_requests", unexpected_disk_read)
    monkeypatch.setattr(store, "get", unexpected_disk_read)
    monkeypatch.setattr(store, "_get_indexed_snapshot", unexpected_disk_read)
    second = collect_real_temperature_batch_from_plan(
        jobs,
        plan,
        store,
        client=None,
        max_new_tasks=0,
        now_utc=now,
    )

    assert second.profiles == first.profiles
    assert all(sample.cache_hit for sample in second.samples)


def test_precomputed_plan_refreshes_misses_published_by_another_process(
    tmp_path,
) -> None:
    jobs = [_job("A", longitude=-112.08, latitude=33.44)]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8), time(12), time(17)),
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    plan = plan_profile_collection(requests, store, now_utc=now)
    assert plan.new_task_count == 3

    for minute, request in requests.items():
        store.publish(
            request,
            activity_id=f"other-process-{minute}",
            collected_at_utc=now,
            temporal_scope="historical",
            raw_result=_result(20 + minute / 60),
        )

    batch = collect_real_temperature_batch_from_plan(
        jobs,
        plan,
        store,
        client=None,
        max_new_tasks=plan.new_task_count,
        now_utc=now,
    )

    assert all(sample.cache_hit for sample in batch.samples)


def test_precomputed_plan_rechecks_integrity_with_a_different_store(tmp_path) -> None:
    jobs = [_job("A", longitude=-112.08, latitude=33.44)]
    requests = build_profile_requests(
        jobs,
        target_date=date(2025, 7, 15),
        sample_times=(time(8),),
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    request = requests[480]
    snapshot = store.publish(
        request,
        activity_id="activity-480",
        collected_at_utc=now,
        temporal_scope="historical",
        raw_result=_result(28),
    )
    plan = plan_profile_collection(requests, store, now_utc=now)
    path = next(
        path
        for path in _main_snapshot_files(tmp_path)
        if path.stem == snapshot.snapshot_id
    )
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["payload"]["raw_result"]["map_data"]["features"][0]["properties"][
        "average_temperature"
    ] = 99
    path.write_text(json.dumps(entry), encoding="utf-8")
    execution_store = HeatmapSnapshotStore(tmp_path)

    with pytest.raises(CacheCorruptionError, match="checksum"):
        collect_real_temperature_batch_from_plan(
            jobs,
            plan,
            execution_store,
            client=None,
            max_new_tasks=0,
            now_utc=now,
        )


def test_profile_request_builder_rejects_pre_hackathon_data_floor() -> None:
    job = _job("A", longitude=-112.08, latitude=33.44)

    with pytest.raises(ValueError, match="2021-01-01"):
        build_profile_requests([job], target_date=date(2020, 12, 31))


def test_profile_request_builder_rejects_duplicate_jobs_before_api_planning() -> None:
    job = _job("A", longitude=-112.08, latitude=33.44)

    with pytest.raises(ValueError, match="job IDs must be unique"):
        build_profile_requests([job, job], target_date=date(2025, 7, 15))


class EmptyResultClient:
    """FortyGuard returns a completed response with no tiles for some dates."""

    def __init__(self) -> None:
        self.calls = 0

    def create_heatmap(self, request, *, poll_interval_seconds=2.0, max_attempts=60):
        del poll_interval_seconds, max_attempts
        self.calls += 1
        return "activity-empty", {
            "map_data": {"type": "FeatureCollection", "features": []}
        }


def test_a_result_with_no_tiles_is_never_cached(tmp_path) -> None:
    """An empty completed response must not poison the date permanently.

    The store is append-only, so caching a zero-tile answer would make every
    later read a cache hit on nothing, with no way to refetch. Failing loudly
    keeps the gap visible.
    """

    from certiroute.fortyguard.errors import FortyGuardProtocolError

    jobs = [
        _job("A", longitude=-112.08, latitude=33.44),
        _job("B", longitude=-112.06, latitude=33.46),
    ]
    store = HeatmapSnapshotStore(tmp_path)
    requests = build_profile_requests(
        jobs, target_date=date(2026, 8, 22), sample_times=(time(8, 0),)
    )
    client = EmptyResultClient()

    with pytest.raises(FortyGuardProtocolError, match="no temperature tiles"):
        collect_real_temperature_batch(
            jobs, requests, store, client=client, max_new_tasks=1
        )

    assert client.calls == 1
    assert _main_snapshot_files(tmp_path) == ()
    # The date stays collectable rather than being permanently answered.
    assert plan_profile_collection(requests, store).new_task_count == 1


def test_a_tile_less_cached_snapshot_is_not_a_cache_hit(tmp_path) -> None:
    """Records written before empty results were rejected must not answer."""

    from certiroute.collection import SnapshotTemporalScope

    jobs = [
        _job("A", longitude=-112.08, latitude=33.44),
        _job("B", longitude=-112.06, latitude=33.46),
    ]
    store = HeatmapSnapshotStore(tmp_path)
    requests = build_profile_requests(
        jobs, target_date=date(2026, 8, 10), sample_times=(time(8, 0),)
    )
    store.publish(
        requests[8 * 60],
        raw_result={"map_data": {"type": "FeatureCollection", "features": []}},
        activity_id="poisoned",
        temporal_scope=SnapshotTemporalScope.HISTORICAL,
        collected_at_utc=datetime(2026, 8, 11, tzinfo=UTC),
    )

    plan = plan_profile_collection(requests, store)

    assert plan.new_task_count == 1
    assert plan.cache_hit_count == 0
