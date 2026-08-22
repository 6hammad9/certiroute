from datetime import UTC, date, datetime, time

import pytest

from certiroute.collection import HeatmapSnapshotStore
from certiroute.domain import GeoPoint, Job
from certiroute.real_conditions import (
    ClusteredProfileRequests,
    ClusteredRequestKey,
    build_clustered_profile_requests,
    collect_clustered_real_temperature_batch_from_plan,
    plan_clustered_profile_collection,
)


def _job(job_id: str, longitude: float, latitude: float = 33.45) -> Job:
    return Job(
        job_id=job_id,
        name=job_id,
        location=GeoPoint(longitude=longitude, latitude=latitude),
        duration_minutes=30,
    )


class ClusterAwareClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, float]] = []

    def create_heatmap(
        self,
        request,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ):
        del poll_interval_seconds, max_attempts
        sample_time = request.date_time.start_time
        minute = sample_time.hour * 60 + sample_time.minute
        geometry = request.polygon_aoi.features[0].geometry
        west = min(position[0] for position in geometry.coordinates[0])
        self.calls.append((minute, west))
        cluster_offset = 5 if west > -112 else 0
        temperature = 20 + sample_time.hour + cluster_offset
        result = {
            "map_data": {
                "type": "FeatureCollection",
                "features": [
                    {
                        "type": "Feature",
                        "properties": {"average_temperature": temperature},
                        "geometry": geometry.model_dump(mode="json"),
                    }
                ],
            }
        }
        return f"activity-{minute}-{west:.4f}", result


def _spread_jobs() -> list[Job]:
    return [
        _job("WEST-1", -112.080),
        _job("EAST-2", -111.895),
        _job("WEST-2", -112.075),
        _job("EAST-1", -111.900),
    ]


def test_builds_complete_request_grid_across_bounded_aois() -> None:
    profile_requests = build_clustered_profile_requests(
        _spread_jobs(),
        target_date=date(2026, 7, 15),
        sample_times=(time(8), time(12)),
        max_aoi_area_square_miles=0.5,
    )

    assert profile_requests.aoi_count == 2
    assert profile_requests.request_count == 4
    assert profile_requests.sample_minutes == (480, 720)
    assert list(profile_requests.requests_by_key) == [
        ClusteredRequestKey(480, 0),
        ClusteredRequestKey(480, 1),
        ClusteredRequestKey(720, 0),
        ClusteredRequestKey(720, 1),
    ]
    assert profile_requests.job_ids_by_aoi == (
        ("WEST-1", "WEST-2"),
        ("EAST-1", "EAST-2"),
    )
    assert all(area <= 0.5 for area in profile_requests.aoi_area_square_miles)
    assert all(
        request.date_time.start_time.hour * 60 + request.date_time.start_time.minute
        == key.minute_of_day
        for key, request in profile_requests.requests_by_key.items()
    )


def test_collects_each_aoi_then_merges_profiles_and_provenance(tmp_path) -> None:
    jobs = _spread_jobs()
    requests = build_clustered_profile_requests(
        jobs,
        target_date=date(2026, 7, 15),
        sample_times=(time(8), time(12)),
        max_aoi_area_square_miles=0.5,
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    plan = plan_clustered_profile_collection(requests, store, now_utc=now)
    client = ClusterAwareClient()

    batch = collect_clustered_real_temperature_batch_from_plan(
        jobs,
        plan,
        store,
        client=client,
        max_new_tasks=plan.new_task_count,
        now_utc=now,
        clock=lambda: now,
    )

    assert len(client.calls) == 4
    assert batch.aoi_count == 2
    assert list(batch.profiles) == [job.job_id for job in jobs]
    assert [point.temperature_c for point in batch.profiles["WEST-1"].points] == [
        28,
        32,
    ]
    assert [point.temperature_c for point in batch.profiles["EAST-1"].points] == [
        33,
        37,
    ]
    assert [(sample.minute_of_day, sample.aoi_index) for sample in batch.samples] == [
        (480, 0),
        (480, 1),
        (720, 0),
        (720, 1),
    ]
    assert all(not sample.cache_hit for sample in batch.samples)
    assert {job_id for sample in batch.samples for job_id in sample.job_ids} == {
        job.job_id for job in jobs
    }


def test_clustered_collection_reuses_every_exact_cached_request(tmp_path) -> None:
    jobs = _spread_jobs()
    requests = build_clustered_profile_requests(
        jobs,
        target_date=date(2026, 7, 15),
        sample_times=(time(8), time(12)),
        max_aoi_area_square_miles=0.5,
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    initial_plan = plan_clustered_profile_collection(requests, store, now_utc=now)
    first = collect_clustered_real_temperature_batch_from_plan(
        jobs,
        initial_plan,
        store,
        client=ClusterAwareClient(),
        max_new_tasks=initial_plan.new_task_count,
        now_utc=now,
        clock=lambda: now,
    )

    cached_plan = plan_clustered_profile_collection(requests, store, now_utc=now)
    assert cached_plan.cache_hit_count == 4
    assert cached_plan.new_task_count == 0
    second = collect_clustered_real_temperature_batch_from_plan(
        jobs,
        cached_plan,
        store,
        client=None,
        max_new_tasks=0,
        now_utc=now,
    )

    assert second.profiles == first.profiles
    assert all(sample.cache_hit for sample in second.samples)


def test_total_missing_task_cap_is_checked_before_any_aoi_request(tmp_path) -> None:
    jobs = _spread_jobs()
    requests = build_clustered_profile_requests(
        jobs,
        target_date=date(2026, 7, 15),
        sample_times=(time(8), time(12)),
        max_aoi_area_square_miles=0.5,
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    plan = plan_clustered_profile_collection(requests, store, now_utc=now)
    client = ClusterAwareClient()

    with pytest.raises(ValueError, match="more new API tasks"):
        collect_clustered_real_temperature_batch_from_plan(
            jobs,
            plan,
            store,
            client=client,
            max_new_tasks=plan.new_task_count - 1,
            now_utc=now,
        )

    assert client.calls == []


def test_jobs_must_match_plan_before_network_collection(tmp_path) -> None:
    jobs = _spread_jobs()
    requests = build_clustered_profile_requests(
        jobs,
        target_date=date(2026, 7, 15),
        sample_times=(time(8),),
        max_aoi_area_square_miles=0.5,
    )
    store = HeatmapSnapshotStore(tmp_path)
    now = datetime(2026, 8, 22, 12, tzinfo=UTC)
    plan = plan_clustered_profile_collection(requests, store, now_utc=now)
    client = ClusterAwareClient()

    with pytest.raises(ValueError, match="jobs do not match clustered requests"):
        collect_clustered_real_temperature_batch_from_plan(
            jobs[:-1],
            plan,
            store,
            client=client,
            max_new_tasks=plan.new_task_count,
            now_utc=now,
        )

    assert client.calls == []


def test_duplicate_coordinates_remain_owned_by_exactly_one_aoi() -> None:
    jobs = [
        _job("SAME-1", -112.08),
        _job("SAME-2", -112.08),
        _job("FAR", -111.90),
    ]

    requests = build_clustered_profile_requests(
        jobs,
        target_date=date(2026, 7, 15),
        sample_times=(time(8),),
        max_aoi_area_square_miles=0.5,
    )

    owners = [job_id for group in requests.job_ids_by_aoi for job_id in group]
    assert sorted(owners) == ["FAR", "SAME-1", "SAME-2"]
    assert len(owners) == len(set(owners))


def test_cluster_limit_cannot_exceed_fortyguard_ten_square_mile_cap() -> None:
    with pytest.raises(ValueError, match="cannot exceed 10"):
        build_clustered_profile_requests(
            [_job("A", -112.08)],
            target_date=date(2026, 7, 15),
            max_aoi_area_square_miles=10.1,
        )


def test_rejects_duplicate_heatmap_request_across_manual_aois(tmp_path) -> None:
    requests = build_clustered_profile_requests(
        _spread_jobs(),
        target_date=date(2026, 7, 15),
        sample_times=(time(8),),
        max_aoi_area_square_miles=0.5,
    )
    requests_by_key = dict(requests.requests_by_key)
    requests_by_key[ClusteredRequestKey(480, 1)] = requests_by_key[
        ClusteredRequestKey(480, 0)
    ]
    duplicate = ClusteredProfileRequests(
        requests_by_key=requests_by_key,
        job_ids_by_aoi=requests.job_ids_by_aoi,
        aoi_area_square_miles=requests.aoi_area_square_miles,
        job_locations=requests.job_locations,
    )

    with pytest.raises(ValueError, match="same heatmap request"):
        plan_clustered_profile_collection(
            duplicate,
            HeatmapSnapshotStore(tmp_path),
            now_utc=datetime(2026, 8, 22, 12, tzinfo=UTC),
        )
