"""Read the one same-day signal FortyGuard actually returns.

Filter type 1 (hourly) returns nothing for the current date, so an hourly
reading of this morning is not obtainable. Filter type 3 returns a whole-day
aggregate and *is* available for today. That single number per tile is
therefore the only way any plan made today can be conditioned on today rather
than on the past, which is what separates a planner from a replay.

This module fetches that aggregate for a set of job sites and returns one
level per site, so each site is anchored on its own local heat rather than on
an area-wide mean.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from certiroute.collection import HeatmapSnapshotStore
from certiroute.domain import Job
from certiroute.fortyguard.client import FortyGuardClient
from certiroute.fortyguard.errors import FortyGuardTaskTimeout
from certiroute.fortyguard.geometry import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    cluster_points_into_aois,
)
from certiroute.fortyguard.heatmap_profiles import (
    heatmap_has_tiles,
    map_job_temperatures,
)
from certiroute.fortyguard.schemas import (
    DailyAggregateDateTime,
    HeatmapRequest,
    PolygonFeatureCollection,
)

# Today's aggregate keeps moving as the day completes, so a cached copy is
# only reused briefly. A finished day is immutable and never expires.
DEFAULT_LIVE_LEVEL_TTL = timedelta(minutes=30)


class DailyLevelUnavailableError(LookupError):
    """FortyGuard has not published a whole-day reading for this date."""


class DailyLevelPendingError(LookupError):
    """The reading is still being computed, and can be resumed.

    A whole-day aggregate is a heavier computation than a single hour and can
    outlast a reasonable interactive wait. That is not a failure: the activity
    is still running on FortyGuard's side. Carrying its identifier lets the
    next attempt rejoin the work already in progress instead of submitting a
    second task and abandoning the first.
    """

    def __init__(self, message: str, *, activity_id: str) -> None:
        super().__init__(message)
        self.activity_id = activity_id


@dataclass(frozen=True)
class DailyLevelReading:
    """One day's aggregate temperature at each job site."""

    target_date: date
    granularity_m: int
    level_by_job: dict[str, float]
    activity_id: str
    collected_at_utc: datetime
    cache_hit: bool

    @property
    def area_mean_c(self) -> float:
        """The area-wide level, used when a single scalar is required."""

        values = list(self.level_by_job.values())
        return sum(values) / len(values)


def build_daily_level_request(
    polygon: PolygonFeatureCollection,
    *,
    target_date: date,
    granularity: int = 60,
) -> HeatmapRequest:
    """Build the whole-day aggregate request for one area and date."""

    return HeatmapRequest(
        polygon_aoi=polygon,
        date_time=DailyAggregateDateTime(start_date=target_date),
        granularity=granularity,
    )


def collect_daily_level(
    jobs: Sequence[Job],
    polygon: PolygonFeatureCollection,
    store: HeatmapSnapshotStore,
    *,
    target_date: date,
    granularity: int = 60,
    client: FortyGuardClient | None = None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    live_ttl: timedelta = DEFAULT_LIVE_LEVEL_TTL,
    resume_activity_id: str | None = None,
    now_utc: datetime | None = None,
) -> DailyLevelReading:
    """Return each site's whole-day level, from cache when one is usable.

    Passing ``client=None`` makes this strictly offline: a missing snapshot
    raises rather than silently producing a level from nothing.
    """

    if not jobs:
        raise ValueError("at least one job is required to read a daily level")
    now = now_utc if now_utc is not None else datetime.now(UTC)
    request = build_daily_level_request(
        polygon, target_date=target_date, granularity=granularity
    )

    # A finished day is immutable; today's aggregate still moves as the day
    # completes, so it is stored under the live scope and re-read each session.
    is_finished_day = target_date < now.date()
    cached = (
        store.lookup_historical(request)
        if is_finished_day
        else store.lookup_current_or_forecast(request, ttl=live_ttl, now_utc=now)
    )
    # A record written before empty results were rejected, or by another tool,
    # must not be trusted just because it exists. Treating it as absent lets
    # the date recover instead of failing for as long as the record survives.
    if cached is not None and not heatmap_has_tiles(cached.raw_result):
        cached = None
    if cached is not None:
        return DailyLevelReading(
            target_date=target_date,
            granularity_m=granularity,
            level_by_job=map_job_temperatures(jobs, cached.raw_result),
            activity_id=cached.activity_id,
            collected_at_utc=cached.collected_at_utc,
            cache_hit=True,
        )
    if client is None:
        raise LookupError(
            f"no cached whole-day aggregate for {target_date.isoformat()} and no "
            "client was supplied to fetch one"
        )

    # Rejoining a task already in flight costs nothing; submitting a second
    # one costs a credit and abandons the first.
    activity_id = resume_activity_id or client.submit_heatmap(request)
    try:
        raw_result = dict(
            client.wait_for_activity(
                activity_id,
                poll_interval_seconds=poll_interval_seconds,
                max_attempts=max_attempts,
            )
        )
    except FortyGuardTaskTimeout as exc:
        raise DailyLevelPendingError(
            f"FortyGuard is still computing the whole-day reading for "
            f"{target_date.isoformat()}.",
            activity_id=activity_id,
        ) from exc
    if not heatmap_has_tiles(raw_result):
        # Early in a day the aggregate is not published yet: the response is
        # successful and empty. Caching it would answer the date with nothing
        # for as long as the record survives, so it is refused here.
        raise DailyLevelUnavailableError(
            f"FortyGuard has not published a whole-day reading for "
            f"{target_date.isoformat()} yet. Nothing was cached."
        )
    stored = store.publish(
        request,
        raw_result=raw_result,
        activity_id=activity_id,
        temporal_scope=(
            "historical" if is_finished_day else "current_or_forecast"
        ),
        collected_at_utc=now,
    )
    return DailyLevelReading(
        target_date=target_date,
        granularity_m=granularity,
        level_by_job=map_job_temperatures(jobs, raw_result),
        activity_id=activity_id,
        collected_at_utc=stored.collected_at_utc,
        cache_hit=False,
    )


def collect_clustered_daily_level(
    jobs: Sequence[Job],
    store: HeatmapSnapshotStore,
    *,
    target_date: date,
    granularity: int = 60,
    client: FortyGuardClient | None = None,
    poll_interval_seconds: float = 5.0,
    max_attempts: int = 60,
    live_ttl: timedelta = DEFAULT_LIVE_LEVEL_TTL,
    resume_activity_ids: Mapping[int, str] | None = None,
    max_aoi_area_square_miles: float = DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    now_utc: datetime | None = None,
) -> DailyLevelReading:
    """Read every site's level, splitting the work into permitted areas.

    FortyGuard caps a single request at 10 square miles. A crew whose sites
    span more than that is ordinary, not exceptional - one bounding box across
    a metro exceeds the cap easily - so the sites are partitioned into compact
    areas exactly as the hourly path already does, and the per-site levels are
    merged back into one reading.
    """

    if not jobs:
        raise ValueError("at least one job is required to read a daily level")
    ordered = sorted(jobs, key=lambda job: (job.location.longitude, job.job_id))
    clusters = cluster_points_into_aois(
        (job.location for job in ordered),
        max_area_square_miles=max_aoi_area_square_miles,
    )

    remaining: defaultdict[tuple[float, float], deque[Job]] = defaultdict(deque)
    for job in ordered:
        remaining[job.location.geojson_position].append(job)

    levels: dict[str, float] = {}
    activity_ids: list[str] = []
    collected: list[datetime] = []
    every_reading_cached = True
    resume = dict(resume_activity_ids or {})
    for index, cluster in enumerate(clusters):
        cluster_jobs = [
            remaining[point.geojson_position].popleft() for point in cluster.points
        ]
        reading = collect_daily_level(
            cluster_jobs,
            cluster.polygon,
            store,
            target_date=target_date,
            granularity=granularity,
            client=client,
            poll_interval_seconds=poll_interval_seconds,
            max_attempts=max_attempts,
            live_ttl=live_ttl,
            resume_activity_id=resume.get(index),
            now_utc=now_utc,
        )
        levels.update(reading.level_by_job)
        activity_ids.append(reading.activity_id)
        collected.append(reading.collected_at_utc)
        every_reading_cached = every_reading_cached and reading.cache_hit

    return DailyLevelReading(
        target_date=target_date,
        granularity_m=granularity,
        level_by_job=levels,
        activity_id=", ".join(activity_ids),
        collected_at_utc=max(collected),
        cache_hit=every_reading_cached,
    )


__all__ = [
    "DEFAULT_LIVE_LEVEL_TTL",
    "DailyLevelPendingError",
    "DailyLevelReading",
    "DailyLevelUnavailableError",
    "build_daily_level_request",
    "collect_clustered_daily_level",
    "collect_daily_level",
]
