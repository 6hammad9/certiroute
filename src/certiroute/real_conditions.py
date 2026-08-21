"""Collect real FortyGuard heatmaps and turn them into scheduler inputs.

This module is the testable boundary between the credit-consuming API client,
the append-only local snapshot store, and CertiRoute's temperature profiles.
It never invents a fallback value: every requested hour must have a completed
snapshot and every job must be covered by a returned temperature tile.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Protocol

from certiroute.collection import (
    HeatmapSnapshot,
    HeatmapSnapshotStore,
    SnapshotTemporalScope,
    heatmap_request_fingerprint,
)
from certiroute.domain import Job
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.fortyguard.heatmap_profiles import build_temperature_profiles
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime
from certiroute.optimization import TemperatureProfile

HACKATHON_DATA_START = date(2021, 1, 1)
DEFAULT_REAL_SAMPLE_TIMES = (time(8, 0), time(12, 0), time(17, 0))
DEFAULT_LIVE_CACHE_TTL = timedelta(minutes=15)
REQUEST_TIME_ASSUMPTION = (
    "The API request wall clock is aligned to the Phoenix crew-shift clock for "
    "this prototype; FortyGuard's heatmap request timezone is not documented."
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


@dataclass(frozen=True)
class HeatmapCollectionPlan:
    """Exact cache-hit and network-task plan for one profile collection."""

    requests_by_minute: dict[int, HeatmapRequest]
    snapshots_by_minute: dict[int, HeatmapSnapshot]
    missing_minutes: tuple[int, ...]
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
class HeatmapSampleProvenance:
    """Operator-visible provenance for one requested hour."""

    minute_of_day: int
    activity_id: str
    collected_at_utc: datetime
    snapshot_id: str
    cache_hit: bool


@dataclass(frozen=True)
class RealTemperatureBatch:
    """Completed real-data profiles plus their request provenance."""

    profiles: dict[str, TemperatureProfile]
    samples: tuple[HeatmapSampleProvenance, ...]
    target_date: date
    granularity: int
    request_time_assumption: str = REQUEST_TIME_ASSUMPTION


def build_profile_requests(
    jobs: Sequence[Job],
    *,
    target_date: date,
    sample_times: Sequence[time] = DEFAULT_REAL_SAMPLE_TIMES,
    granularity: int = 100,
) -> dict[int, HeatmapRequest]:
    """Build one bounded AOI heatmap request for every selected sample hour."""

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
    if not sample_times:
        raise ValueError("at least one sample time is required")

    polygon = bounding_polygon(job.location for job in jobs)
    requests: dict[int, HeatmapRequest] = {}
    for sample_time in sample_times:
        if sample_time.tzinfo is not None:
            raise ValueError("sample times must be unzoned wall-clock values")
        if sample_time.second or sample_time.microsecond:
            raise ValueError("sample time precision is one minute")
        minute = sample_time.hour * 60 + sample_time.minute
        if minute in requests:
            raise ValueError("sample times cannot contain duplicate minutes")
        requests[minute] = HeatmapRequest(
            polygon_aoi=polygon,
            date_time=SingleHourDateTime(
                start_date=target_date,
                start_time=sample_time,
            ),
            granularity=granularity,
        )
    return dict(sorted(requests.items()))


def plan_profile_collection(
    requests_by_minute: Mapping[int, HeatmapRequest],
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
    snapshots: dict[int, HeatmapSnapshot] = {}
    missing: list[int] = []
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
        collected_at = _utc_now(get_now())
        snapshots[minute] = store.publish(
            request,
            activity_id=activity_id,
            collected_at_utc=collected_at,
            temporal_scope=_temporal_scope(request, now_utc=collected_at),
            raw_result=result,
        )

    ordered_snapshots = dict(sorted(snapshots.items()))
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
    "HeatmapCollectionPlan",
    "HeatmapSampleProvenance",
    "RealTemperatureBatch",
    "build_profile_requests",
    "collect_real_temperature_batch",
    "collect_real_temperature_batch_from_plan",
    "plan_profile_collection",
]
