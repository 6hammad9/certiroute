"""Collect real FortyGuard heatmaps and turn them into scheduler inputs.

This module is the testable boundary between the credit-consuming API client,
the append-only local snapshot store, and CertiRoute's temperature profiles.
It never invents a fallback value: every requested hour must have a completed
snapshot and every job must be covered by a returned temperature tile.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol, cast

from certiroute.collection import (
    HeatmapSnapshot,
    HeatmapSnapshotStore,
    SnapshotTemporalScope,
    heatmap_request_fingerprint,
)
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard.errors import FortyGuardProtocolError
from certiroute.fortyguard.geometry import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    bounding_polygon,
    cluster_points_into_aois,
    validate_aoi_area,
)
from certiroute.fortyguard.heatmap_profiles import (
    build_temperature_profiles,
    geometry_covers_point,
)
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime
from certiroute.optimization import TemperatureProfile

HACKATHON_DATA_START = date(2021, 1, 1)
DEFAULT_REAL_SAMPLE_TIMES = (time(8, 0), time(12, 0), time(17, 0))
DEFAULT_LIVE_CACHE_TTL = timedelta(minutes=15)
REQUEST_TIME_ASSUMPTION = (
    "The API request wall clock is aligned to the entered crew-shift clock for "
    "this historical replay; FortyGuard's heatmap request timezone is not documented."
)


class HeatmapCreator(Protocol):
    """The subset of ``FortyGuardClient`` used by the collection workflow."""

    def create_heatmap(
        self,
        request: HeatmapRequest,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ) -> tuple[str, Mapping[str, Any]]: ...


@dataclass(frozen=True, order=True)
class ClusteredRequestKey:
    """Stable identity for one sample time within one bounded AOI."""

    minute_of_day: int
    aoi_index: int

    def __post_init__(self) -> None:
        if isinstance(self.minute_of_day, bool) or not isinstance(
            self.minute_of_day, int
        ):
            raise TypeError("minute_of_day must be an integer")
        if not 0 <= self.minute_of_day < 24 * 60:
            raise ValueError("minute_of_day must be between 0 and 1439")
        if isinstance(self.aoi_index, bool) or not isinstance(self.aoi_index, int):
            raise TypeError("aoi_index must be an integer")
        if self.aoi_index < 0:
            raise ValueError("aoi_index cannot be negative")


CollectionRequestKey = int | ClusteredRequestKey


@dataclass(frozen=True)
class ClusteredProfileRequests:
    """Temperature requests and job ownership for one or more bounded AOIs."""

    requests_by_key: dict[ClusteredRequestKey, HeatmapRequest]
    job_ids_by_aoi: tuple[tuple[str, ...], ...]
    aoi_area_square_miles: tuple[float, ...]
    job_locations: dict[str, GeoPoint]

    @property
    def request_count(self) -> int:
        return len(self.requests_by_key)

    @property
    def aoi_count(self) -> int:
        return len(self.job_ids_by_aoi)

    @property
    def sample_minutes(self) -> tuple[int, ...]:
        return tuple(sorted({key.minute_of_day for key in self.requests_by_key}))


@dataclass(frozen=True)
class HeatmapCollectionPlan:
    """Exact cache-hit and network-task plan for one profile collection."""

    requests_by_minute: dict[CollectionRequestKey, HeatmapRequest]
    snapshots_by_minute: dict[CollectionRequestKey, HeatmapSnapshot]
    missing_minutes: tuple[CollectionRequestKey, ...]
    store_validation_token: object = field(repr=False, compare=False)

    @property
    def request_count(self) -> int:
        return len(self.requests_by_minute)

    @property
    def cache_hit_count(self) -> int:
        return len(self.snapshots_by_minute)

    @property
    def new_task_count(self) -> int:
        return len(self.missing_minutes)


@dataclass(frozen=True)
class ClusteredHeatmapCollectionPlan:
    """Cache and API-task plan for a clustered profile request set."""

    profile_requests: ClusteredProfileRequests
    collection_plan: HeatmapCollectionPlan

    @property
    def requests_by_key(self) -> dict[ClusteredRequestKey, HeatmapRequest]:
        return self.profile_requests.requests_by_key

    @property
    def snapshots_by_key(self) -> dict[ClusteredRequestKey, HeatmapSnapshot]:
        return cast(
            dict[ClusteredRequestKey, HeatmapSnapshot],
            self.collection_plan.snapshots_by_minute,
        )

    @property
    def missing_keys(self) -> tuple[ClusteredRequestKey, ...]:
        return cast(
            tuple[ClusteredRequestKey, ...],
            self.collection_plan.missing_minutes,
        )

    @property
    def request_count(self) -> int:
        return self.collection_plan.request_count

    @property
    def cache_hit_count(self) -> int:
        return self.collection_plan.cache_hit_count

    @property
    def new_task_count(self) -> int:
        return self.collection_plan.new_task_count

    @property
    def aoi_count(self) -> int:
        return self.profile_requests.aoi_count


@dataclass(frozen=True)
class HeatmapSampleProvenance:
    """Operator-visible provenance for one requested hour."""

    minute_of_day: int
    activity_id: str
    collected_at_utc: datetime
    snapshot_id: str
    cache_hit: bool
    aoi_index: int = 0
    job_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RealTemperatureBatch:
    """Completed real-data profiles plus their request provenance."""

    profiles: dict[str, TemperatureProfile]
    samples: tuple[HeatmapSampleProvenance, ...]
    target_date: date
    granularity: int
    request_time_assumption: str = REQUEST_TIME_ASSUMPTION
    aoi_count: int = 1


def build_profile_requests(
    jobs: Sequence[Job],
    *,
    target_date: date,
    sample_times: Sequence[time] = DEFAULT_REAL_SAMPLE_TIMES,
    granularity: int = 100,
) -> dict[int, HeatmapRequest]:
    """Build one bounded AOI heatmap request for every selected sample hour."""

    _validate_profile_jobs_and_date(jobs, target_date=target_date)
    samples = _validated_sample_times(sample_times)

    polygon = bounding_polygon(job.location for job in jobs)
    requests: dict[int, HeatmapRequest] = {}
    for minute, sample_time in samples:
        requests[minute] = HeatmapRequest(
            polygon_aoi=polygon,
            date_time=SingleHourDateTime(
                start_date=target_date,
                start_time=sample_time,
            ),
            granularity=granularity,
        )
    return dict(sorted(requests.items()))


def build_clustered_profile_requests(
    jobs: Sequence[Job],
    *,
    target_date: date,
    sample_times: Sequence[time] = DEFAULT_REAL_SAMPLE_TIMES,
    granularity: int = 100,
    max_aoi_area_square_miles: float = DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
) -> ClusteredProfileRequests:
    """Build bounded heatmap requests for jobs spread across multiple areas.

    FortyGuard's per-request AOI limit is treated as a batching constraint,
    not as a radius restriction on the whole route. Jobs are deterministically
    partitioned into compact AOIs, and every AOI receives one request for every
    requested sample minute.
    """

    _validate_profile_jobs_and_date(jobs, target_date=target_date)
    samples = _validated_sample_times(sample_times)
    if max_aoi_area_square_miles > DEFAULT_MAX_AOI_AREA_SQUARE_MILES:
        raise ValueError("max_aoi_area_square_miles cannot exceed 10")
    ordered_jobs = tuple(
        sorted(
            jobs,
            key=lambda job: (
                job.location.longitude,
                job.location.latitude,
                job.job_id,
            ),
        )
    )
    clusters = cluster_points_into_aois(
        (job.location for job in ordered_jobs),
        max_area_square_miles=max_aoi_area_square_miles,
    )
    remaining_indices_by_position: defaultdict[tuple[float, float], deque[int]] = (
        defaultdict(deque)
    )
    for index, job in enumerate(ordered_jobs):
        remaining_indices_by_position[job.location.geojson_position].append(index)

    requests: dict[ClusteredRequestKey, HeatmapRequest] = {}
    job_ids_by_aoi: list[tuple[str, ...]] = []
    areas: list[float] = []
    for aoi_index, cluster in enumerate(clusters):
        cluster_jobs = tuple(
            ordered_jobs[
                remaining_indices_by_position[point.geojson_position].popleft()
            ]
            for point in cluster.points
        )
        job_ids_by_aoi.append(tuple(job.job_id for job in cluster_jobs))
        areas.append(cluster.area_square_miles)
        for minute, sample_time in samples:
            key = ClusteredRequestKey(
                minute_of_day=minute,
                aoi_index=aoi_index,
            )
            requests[key] = HeatmapRequest(
                polygon_aoi=cluster.polygon,
                date_time=SingleHourDateTime(
                    start_date=target_date,
                    start_time=sample_time,
                ),
                granularity=granularity,
            )

    profile_requests = ClusteredProfileRequests(
        requests_by_key=dict(sorted(requests.items())),
        job_ids_by_aoi=tuple(job_ids_by_aoi),
        aoi_area_square_miles=tuple(areas),
        job_locations={job.job_id: job.location for job in ordered_jobs},
    )
    _validate_clustered_profile_requests(profile_requests)
    return profile_requests


def plan_profile_collection(
    requests_by_minute: Mapping[CollectionRequestKey, HeatmapRequest],
    store: HeatmapSnapshotStore,
    *,
    now_utc: datetime | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> HeatmapCollectionPlan:
    """Resolve exact cache hits without submitting any network requests."""

    now = _utc_now(now_utc)
    requests = dict(sorted(requests_by_minute.items()))
    fingerprints_by_minute = {
        minute: heatmap_request_fingerprint(request)
        for minute, request in requests.items()
    }
    candidates_by_fingerprint = store.list_for_requests(requests.values())
    snapshots: dict[CollectionRequestKey, HeatmapSnapshot] = {}
    missing: list[CollectionRequestKey] = []
    for minute, request in requests.items():
        snapshot = _select_reusable_snapshot(
            request,
            candidates_by_fingerprint[fingerprints_by_minute[minute]],
            now_utc=now,
            live_ttl=live_ttl,
        )
        if snapshot is None:
            missing.append(minute)
        else:
            snapshots[minute] = snapshot
    return HeatmapCollectionPlan(
        requests_by_minute=requests,
        snapshots_by_minute=snapshots,
        missing_minutes=tuple(missing),
        store_validation_token=store.validation_token,
    )


def plan_clustered_profile_collection(
    profile_requests: ClusteredProfileRequests,
    store: HeatmapSnapshotStore,
    *,
    now_utc: datetime | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> ClusteredHeatmapCollectionPlan:
    """Resolve all AOI/sample cache hits without making network requests."""

    _validate_clustered_profile_requests(profile_requests)
    plan = plan_profile_collection(
        profile_requests.requests_by_key,
        store,
        now_utc=now_utc,
        live_ttl=live_ttl,
    )
    return ClusteredHeatmapCollectionPlan(
        profile_requests=profile_requests,
        collection_plan=plan,
    )


def collect_real_temperature_batch(
    jobs: Sequence[Job],
    requests_by_minute: Mapping[int, HeatmapRequest],
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    max_new_tasks: int,
    now_utc: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> RealTemperatureBatch:
    """Load cached samples and submit no more than the confirmed task count.

    A caller must pass ``max_new_tasks`` from the confirmation shown to the
    operator. If more misses exist at execution time, collection stops before
    making a request. Completed samples are published immediately so a partial
    external failure can be resumed without spending credits twice.
    """

    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")
    if not requests_by_minute:
        raise ValueError("requests_by_minute must contain at least one request")
    now = _utc_now(now_utc)
    plan = plan_profile_collection(
        requests_by_minute,
        store,
        now_utc=now,
        live_ttl=live_ttl,
    )
    return _collect_from_plan(
        jobs,
        plan,
        store,
        client=client,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_attempts,
        max_new_tasks=max_new_tasks,
        clock=clock,
    )


def collect_real_temperature_batch_from_plan(
    jobs: Sequence[Job],
    plan: HeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    max_new_tasks: int,
    now_utc: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> RealTemperatureBatch:
    """Execute a previously displayed plan without redundantly rescanning it.

    Fully cached plans validated by the same in-process store reuse those
    immutable snapshot objects instead of parsing large heatmaps twice. Plans
    with misses or a different store instance are refreshed through the
    persistent request index. A current/forecast hit that expired meanwhile
    becomes a miss and is checked against ``max_new_tasks`` before any API call.
    """

    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")
    now = _utc_now(now_utc)
    _validate_collection_plan(plan)
    refreshed_plan = _refresh_collection_plan(
        plan,
        store,
        now_utc=now,
        live_ttl=live_ttl,
    )
    return _collect_from_plan(
        jobs,
        refreshed_plan,
        store,
        client=client,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_attempts,
        max_new_tasks=max_new_tasks,
        clock=clock,
    )


def collect_clustered_real_temperature_batch(
    jobs: Sequence[Job],
    profile_requests: ClusteredProfileRequests,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    max_new_tasks: int,
    now_utc: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> RealTemperatureBatch:
    """Collect and merge real profiles from every bounded AOI in a route."""

    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")
    _validate_clustered_jobs(jobs, profile_requests)
    plan = plan_clustered_profile_collection(
        profile_requests,
        store,
        now_utc=now_utc,
        live_ttl=live_ttl,
    )
    return collect_clustered_real_temperature_batch_from_plan(
        jobs,
        plan,
        store,
        client=client,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_attempts,
        max_new_tasks=max_new_tasks,
        now_utc=now_utc,
        clock=clock,
        live_ttl=live_ttl,
    )


def collect_clustered_real_temperature_batch_from_plan(
    jobs: Sequence[Job],
    plan: ClusteredHeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    max_new_tasks: int,
    now_utc: datetime | None = None,
    clock: Callable[[], datetime] | None = None,
    live_ttl: timedelta = DEFAULT_LIVE_CACHE_TTL,
) -> RealTemperatureBatch:
    """Execute an operator-confirmed multi-AOI collection plan."""

    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")
    _validate_clustered_jobs(jobs, plan.profile_requests)
    _validate_clustered_collection_plan(plan)
    now = _utc_now(now_utc)
    refreshed = _refresh_collection_plan(
        plan.collection_plan,
        store,
        now_utc=now,
        live_ttl=live_ttl,
    )
    snapshots, cache_hits = _collect_snapshots_from_plan(
        refreshed,
        store,
        client=client,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_attempts,
        max_new_tasks=max_new_tasks,
        clock=clock,
    )
    return _build_clustered_batch(
        jobs,
        plan.profile_requests,
        snapshots,
        cache_hits=cache_hits,
    )


def _collect_from_plan(
    jobs: Sequence[Job],
    plan: HeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float,
    max_attempts: int,
    max_new_tasks: int,
    clock: Callable[[], datetime] | None,
) -> RealTemperatureBatch:
    snapshots, cache_hits = _collect_snapshots_from_plan(
        plan,
        store,
        client=client,
        poll_interval_seconds=poll_interval_seconds,
        max_attempts=max_attempts,
        max_new_tasks=max_new_tasks,
        clock=clock,
    )
    if not all(isinstance(key, int) and not isinstance(key, bool) for key in snapshots):
        raise TypeError("single-AOI collection keys must be integer minutes")
    ordered_snapshots: dict[int, HeatmapSnapshot] = {
        int(key): snapshot for key, snapshot in sorted(snapshots.items())
    }
    profiles = build_temperature_profiles(
        jobs,
        {minute: snapshot.raw_result for minute, snapshot in ordered_snapshots.items()},
    )
    first_request = next(iter(plan.requests_by_minute.values()))
    return RealTemperatureBatch(
        profiles=profiles,
        samples=tuple(
            HeatmapSampleProvenance(
                minute_of_day=minute,
                activity_id=snapshot.activity_id,
                collected_at_utc=snapshot.collected_at_utc,
                snapshot_id=snapshot.snapshot_id,
                cache_hit=minute in cache_hits,
            )
            for minute, snapshot in ordered_snapshots.items()
        ),
        target_date=first_request.date_time.start_date,
        granularity=first_request.granularity,
    )


def _collect_snapshots_from_plan(
    plan: HeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    poll_interval_seconds: float,
    max_attempts: int,
    max_new_tasks: int,
    clock: Callable[[], datetime] | None,
) -> tuple[dict[CollectionRequestKey, HeatmapSnapshot], set[CollectionRequestKey]]:
    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")
    if not plan.requests_by_minute:
        raise ValueError("requests_by_minute must contain at least one request")
    if plan.new_task_count > max_new_tasks:
        raise ValueError(
            "collection requires more new API tasks than the operator confirmed"
        )
    if plan.missing_minutes and client is None:
        raise ValueError("an API client is required for uncached heatmap requests")

    snapshots = dict(plan.snapshots_by_minute)
    cache_hits = set(snapshots)
    get_now = clock or (lambda: datetime.now(UTC))
    for minute in plan.missing_minutes:
        request = plan.requests_by_minute[minute]
        assert client is not None
        activity_id, result = client.create_heatmap(
            request,
            poll_interval_seconds=poll_interval_seconds,
            max_attempts=max_attempts,
        )
        _reject_empty_result(request, result)
        collected_at = _utc_now(get_now())
        snapshots[minute] = store.publish(
            request,
            activity_id=activity_id,
            collected_at_utc=collected_at,
            temporal_scope=_temporal_scope(request, now_utc=collected_at),
            raw_result=result,
        )

    return dict(sorted(snapshots.items())), cache_hits


def _reject_empty_result(
    request: HeatmapRequest, result: Mapping[str, Any]
) -> None:
    """Refuse to archive a completed response that carries no tiles.

    FortyGuard returns a well-formed, "completed" result with zero features
    for dates whose hourly data is not published yet - the immediately
    preceding day behaves this way. Caching that as evidence poisons the date
    permanently, because the store is append-only and every later read is a
    cache hit on an empty answer. Failing here keeps the gap visible and
    re-fetchable.
    """

    features = result.get("map_data", {})
    if isinstance(features, Mapping):
        features = features.get("features")
    if isinstance(features, Sequence) and not isinstance(features, str | bytes):
        if features:
            return
    else:
        return
    moment = request.date_time
    raise FortyGuardProtocolError(
        f"FortyGuard returned no temperature tiles for "
        f"{moment.start_date.isoformat()} {moment.start_time:%H:%M}. Hourly "
        "data for this date is not available yet; nothing was cached."
    )


def _build_clustered_batch(
    jobs: Sequence[Job],
    profile_requests: ClusteredProfileRequests,
    snapshots: Mapping[CollectionRequestKey, HeatmapSnapshot],
    *,
    cache_hits: set[CollectionRequestKey],
) -> RealTemperatureBatch:
    expected_keys = set(profile_requests.requests_by_key)
    if set(snapshots) != expected_keys:
        raise ValueError("collected snapshots do not match the clustered request set")
    if not all(isinstance(key, ClusteredRequestKey) for key in snapshots):
        raise TypeError("clustered collection keys must include minute and AOI")

    jobs_by_id = {job.job_id: job for job in jobs}
    merged_profiles: dict[str, TemperatureProfile] = {}
    for aoi_index, job_ids in enumerate(profile_requests.job_ids_by_aoi):
        cluster_jobs = [jobs_by_id[job_id] for job_id in job_ids]
        results_by_minute = {
            key.minute_of_day: snapshots[key].raw_result
            for key in sorted(profile_requests.requests_by_key)
            if key.aoi_index == aoi_index
        }
        cluster_profiles = build_temperature_profiles(
            cluster_jobs,
            results_by_minute,
        )
        overlap = set(merged_profiles) & set(cluster_profiles)
        if overlap:
            raise ValueError(
                "jobs cannot belong to more than one AOI: " + ", ".join(sorted(overlap))
            )
        merged_profiles.update(cluster_profiles)

    if set(merged_profiles) != set(jobs_by_id):
        missing = sorted(set(jobs_by_id) - set(merged_profiles))
        extra = sorted(set(merged_profiles) - set(jobs_by_id))
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        raise ValueError(
            "clustered profiles do not match jobs (" + "; ".join(details) + ")"
        )

    ordered_keys = tuple(sorted(profile_requests.requests_by_key))
    first_request = profile_requests.requests_by_key[ordered_keys[0]]
    return RealTemperatureBatch(
        profiles={job.job_id: merged_profiles[job.job_id] for job in jobs},
        samples=tuple(
            HeatmapSampleProvenance(
                minute_of_day=key.minute_of_day,
                activity_id=snapshots[key].activity_id,
                collected_at_utc=snapshots[key].collected_at_utc,
                snapshot_id=snapshots[key].snapshot_id,
                cache_hit=key in cache_hits,
                aoi_index=key.aoi_index,
                job_ids=profile_requests.job_ids_by_aoi[key.aoi_index],
            )
            for key in ordered_keys
        ),
        target_date=first_request.date_time.start_date,
        granularity=first_request.granularity,
        aoi_count=profile_requests.aoi_count,
    )


def _select_reusable_snapshot(
    request: HeatmapRequest,
    candidates: Sequence[HeatmapSnapshot],
    *,
    now_utc: datetime,
    live_ttl: timedelta,
) -> HeatmapSnapshot | None:
    scope = _temporal_scope(request, now_utc=now_utc)
    if scope is SnapshotTemporalScope.HISTORICAL:
        historical = [
            snapshot
            for snapshot in candidates
            if snapshot.temporal_scope is SnapshotTemporalScope.HISTORICAL
        ]
        return historical[-1] if historical else None
    if not isinstance(live_ttl, timedelta) or live_ttl <= timedelta(0):
        raise ValueError("ttl must be an explicit positive timedelta")
    for snapshot in reversed(candidates):
        if snapshot.temporal_scope is not SnapshotTemporalScope.CURRENT_OR_FORECAST:
            continue
        age = now_utc - snapshot.collected_at_utc
        if timedelta(0) <= age <= live_ttl:
            return snapshot
    return None


def _validate_collection_plan(plan: HeatmapCollectionPlan) -> None:
    request_minutes = set(plan.requests_by_minute)
    snapshot_minutes = set(plan.snapshots_by_minute)
    missing_minutes = tuple(plan.missing_minutes)
    if not request_minutes:
        raise ValueError("collection plan must contain at least one request")
    if missing_minutes != tuple(sorted(set(missing_minutes))):
        raise ValueError("collection plan missing minutes must be unique and sorted")
    if snapshot_minutes & set(missing_minutes):
        raise ValueError("collection plan cannot mark a minute both cached and missing")
    if snapshot_minutes | set(missing_minutes) != request_minutes:
        raise ValueError("collection plan must account for every requested minute")
    for minute, snapshot in plan.snapshots_by_minute.items():
        expected = heatmap_request_fingerprint(plan.requests_by_minute[minute])
        if snapshot.request_fingerprint != expected:
            raise ValueError("collection plan snapshot does not match its request")


def _refresh_collection_plan(
    plan: HeatmapCollectionPlan,
    store: HeatmapSnapshotStore,
    *,
    now_utc: datetime,
    live_ttl: timedelta,
) -> HeatmapCollectionPlan:
    # A plan with misses needs a fresh indexed lookup to pick up snapshots
    # another process may have published before execution starts. A plan from a
    # different store instance is also revalidated rather than trusted.
    if (
        plan.missing_minutes
        or plan.store_validation_token is not store.validation_token
    ):
        return plan_profile_collection(
            plan.requests_by_minute,
            store,
            now_utc=now_utc,
            live_ttl=live_ttl,
        )

    # The snapshots were checksum-validated by this exact store instance during
    # planning. Reusing those frozen model objects avoids parsing multi-megabyte
    # heatmaps twice in one UI run. Current/forecast TTL is still checked at the
    # execution clock before they are accepted.
    for minute, planned_snapshot in plan.snapshots_by_minute.items():
        reusable = _select_reusable_snapshot(
            plan.requests_by_minute[minute],
            (planned_snapshot,),
            now_utc=now_utc,
            live_ttl=live_ttl,
        )
        if reusable is None:
            return plan_profile_collection(
                plan.requests_by_minute,
                store,
                now_utc=now_utc,
                live_ttl=live_ttl,
            )
    return plan


def _validate_profile_jobs_and_date(jobs: Sequence[Job], *, target_date: date) -> None:
    if not jobs:
        raise ValueError("at least one job is required")
    job_ids = [job.job_id for job in jobs]
    duplicate_ids = sorted(
        job_id for job_id in set(job_ids) if job_ids.count(job_id) > 1
    )
    if duplicate_ids:
        raise ValueError("job IDs must be unique: " + ", ".join(duplicate_ids))
    if target_date < HACKATHON_DATA_START:
        raise ValueError("target_date cannot precede 2021-01-01")


def _validated_sample_times(
    sample_times: Sequence[time],
) -> tuple[tuple[int, time], ...]:
    if not sample_times:
        raise ValueError("at least one sample time is required")
    samples: dict[int, time] = {}
    for sample_time in sample_times:
        if not isinstance(sample_time, time):
            raise TypeError("sample times must be datetime.time values")
        if sample_time.tzinfo is not None:
            raise ValueError("sample times must be unzoned wall-clock values")
        if sample_time.second or sample_time.microsecond:
            raise ValueError("sample time precision is one minute")
        minute = sample_time.hour * 60 + sample_time.minute
        if minute in samples:
            raise ValueError("sample times cannot contain duplicate minutes")
        samples[minute] = sample_time
    return tuple(sorted(samples.items()))


def _validate_clustered_profile_requests(
    profile_requests: ClusteredProfileRequests,
) -> None:
    if not profile_requests.requests_by_key:
        raise ValueError("clustered requests must contain at least one request")
    if not profile_requests.job_ids_by_aoi:
        raise ValueError("clustered requests must contain at least one AOI")
    if len(profile_requests.aoi_area_square_miles) != len(
        profile_requests.job_ids_by_aoi
    ):
        raise ValueError("every AOI must have one reported area")

    flattened_ids = [
        job_id for job_ids in profile_requests.job_ids_by_aoi for job_id in job_ids
    ]
    if any(not job_ids for job_ids in profile_requests.job_ids_by_aoi):
        raise ValueError("every AOI must own at least one job")
    duplicate_ids = sorted(
        job_id for job_id in set(flattened_ids) if flattened_ids.count(job_id) > 1
    )
    if duplicate_ids:
        raise ValueError(
            "jobs cannot belong to more than one AOI: " + ", ".join(duplicate_ids)
        )
    if set(profile_requests.job_locations) != set(flattened_ids):
        raise ValueError("every clustered job must have exactly one saved location")

    keys = tuple(profile_requests.requests_by_key)
    if not all(isinstance(key, ClusteredRequestKey) for key in keys):
        raise TypeError("clustered request keys must include minute and AOI")
    expected_aoi_indices = set(range(profile_requests.aoi_count))
    actual_aoi_indices = {key.aoi_index for key in keys}
    if actual_aoi_indices != expected_aoi_indices:
        raise ValueError("clustered request AOI indices must be contiguous from zero")
    sample_minutes = {key.minute_of_day for key in keys}
    expected_keys = {
        ClusteredRequestKey(minute, aoi_index)
        for minute in sample_minutes
        for aoi_index in expected_aoi_indices
    }
    if set(keys) != expected_keys:
        raise ValueError("every AOI must have a request for every sample minute")

    target_dates: set[date] = set()
    granularities: set[int] = set()
    polygon_by_aoi: dict[int, str] = {}
    polygon_request_by_aoi: dict[int, HeatmapRequest] = {}
    actual_area_by_aoi: dict[int, float] = {}
    fingerprint_owner: dict[tuple[int, str], int] = {}
    for key, request in profile_requests.requests_by_key.items():
        request_time = request.date_time.start_time
        request_minute = request_time.hour * 60 + request_time.minute
        if request_minute != key.minute_of_day:
            raise ValueError("clustered request key does not match request time")
        target_dates.add(request.date_time.start_date)
        granularities.add(request.granularity)
        fingerprint_key = (key.minute_of_day, heatmap_request_fingerprint(request))
        if fingerprint_key in fingerprint_owner:
            raise ValueError("different AOIs cannot contain the same heatmap request")
        fingerprint_owner[fingerprint_key] = key.aoi_index
        polygon_json = request.polygon_aoi.model_dump_json()
        expected_polygon = polygon_by_aoi.setdefault(key.aoi_index, polygon_json)
        if polygon_json != expected_polygon:
            raise ValueError("an AOI must use the same polygon at every sample time")
        polygon_request_by_aoi.setdefault(key.aoi_index, request)
        actual_area_by_aoi[key.aoi_index] = validate_aoi_area(
            request.polygon_aoi,
            max_area_square_miles=DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
        )
    if len(target_dates) != 1:
        raise ValueError("clustered requests must use one target date")
    if len(granularities) != 1:
        raise ValueError("clustered requests must use one granularity")

    for aoi_index, reported_area in enumerate(profile_requests.aoi_area_square_miles):
        if reported_area <= 0 or reported_area > DEFAULT_MAX_AOI_AREA_SQUARE_MILES:
            raise ValueError(
                "reported AOI areas must be greater than zero and at most 10"
            )
        if aoi_index not in polygon_by_aoi:
            raise ValueError("every reported AOI must have requests")
        actual_area = actual_area_by_aoi[aoi_index]
        if abs(actual_area - reported_area) > max(1e-9, actual_area * 1e-9):
            raise ValueError("reported AOI area does not match its request polygon")
        polygon_request = polygon_request_by_aoi[aoi_index]
        for job_id in profile_requests.job_ids_by_aoi[aoi_index]:
            location = profile_requests.job_locations[job_id]
            if not any(
                geometry_covers_point(
                    feature.geometry.model_dump(mode="json"),
                    location,
                )
                for feature in polygon_request.polygon_aoi.features
            ):
                raise ValueError(f"AOI does not cover its assigned job: {job_id}")


def _validate_clustered_jobs(
    jobs: Sequence[Job], profile_requests: ClusteredProfileRequests
) -> None:
    _validate_clustered_profile_requests(profile_requests)
    job_ids = [job.job_id for job in jobs]
    duplicates = sorted(job_id for job_id in set(job_ids) if job_ids.count(job_id) > 1)
    if duplicates:
        raise ValueError("job IDs must be unique: " + ", ".join(duplicates))
    expected = {
        job_id for job_ids in profile_requests.job_ids_by_aoi for job_id in job_ids
    }
    actual = set(job_ids)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append("missing: " + ", ".join(missing))
        if extra:
            details.append("unexpected: " + ", ".join(extra))
        raise ValueError(
            "jobs do not match clustered requests (" + "; ".join(details) + ")"
        )
    moved = sorted(
        job.job_id
        for job in jobs
        if job.location != profile_requests.job_locations[job.job_id]
    )
    if moved:
        raise ValueError(
            "job locations changed after clustered planning: " + ", ".join(moved)
        )


def _validate_clustered_collection_plan(
    plan: ClusteredHeatmapCollectionPlan,
) -> None:
    _validate_clustered_profile_requests(plan.profile_requests)
    _validate_collection_plan(plan.collection_plan)
    if plan.collection_plan.requests_by_minute != plan.profile_requests.requests_by_key:
        raise ValueError("collection plan does not match its clustered requests")


def _temporal_scope(
    request: HeatmapRequest, *, now_utc: datetime
) -> SnapshotTemporalScope:
    # Treat the entire current UTC date conservatively as mutable because the
    # API's request timezone is not documented. Only earlier dates are immutable.
    if request.date_time.start_date < now_utc.date():
        return SnapshotTemporalScope.HISTORICAL
    return SnapshotTemporalScope.CURRENT_OR_FORECAST


def _utc_now(value: datetime | None) -> datetime:
    now = value if value is not None else datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("timestamps must be timezone-aware")
    return now.astimezone(UTC)


__all__ = [
    "DEFAULT_LIVE_CACHE_TTL",
    "DEFAULT_REAL_SAMPLE_TIMES",
    "HACKATHON_DATA_START",
    "REQUEST_TIME_ASSUMPTION",
    "ClusteredHeatmapCollectionPlan",
    "ClusteredProfileRequests",
    "ClusteredRequestKey",
    "HeatmapCollectionPlan",
    "HeatmapSampleProvenance",
    "RealTemperatureBatch",
    "build_clustered_profile_requests",
    "build_profile_requests",
    "collect_clustered_real_temperature_batch",
    "collect_clustered_real_temperature_batch_from_plan",
    "collect_real_temperature_batch",
    "collect_real_temperature_batch_from_plan",
    "plan_clustered_profile_collection",
    "plan_profile_collection",
]
