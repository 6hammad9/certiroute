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

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from certiroute.collection import HeatmapSnapshotStore
from certiroute.domain import Job
from certiroute.fortyguard.client import FortyGuardClient
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

    activity_id = client.submit_heatmap(request)
    raw_result = dict(
        client.wait_for_activity(
            activity_id,
            poll_interval_seconds=poll_interval_seconds,
            max_attempts=max_attempts,
        )
    )
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


__all__ = [
    "DEFAULT_LIVE_LEVEL_TTL",
    "DailyLevelReading",
    "DailyLevelUnavailableError",
    "build_daily_level_request",
    "collect_daily_level",
]
