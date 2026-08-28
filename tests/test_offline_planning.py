"""A day this repository ships must plan end to end with no API call.

The whole-day aggregate is a same-day signal: a day not captured while it was
current can never be bought again, and a deployment starts with an empty cache
every time it restarts. Without committed levels, anyone opening the live link
on a later date meets a refusal about a number that no longer exists to buy.
"""

from datetime import time
from pathlib import Path

import pandas as pd
import pytest

from certiroute.climatology import load_climatology
from certiroute.domain import GeoPoint, Job
from certiroute.measured import (
    DEFAULT_LEVEL_PATH,
    DEFAULT_PROFILE_PATH,
    available_days,
    level_days,
    load_measured_level,
)
from certiroute.same_day import build_same_day_plan

ROOT = Path(__file__).resolve().parents[1]
DEPOT = GeoPoint(latitude=33.44855, longitude=-112.07391)


@pytest.fixture
def phoenix_jobs() -> list[Job]:
    frame = pd.read_csv(ROOT / "data" / "sample" / "phoenix_jobs.csv")
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


def test_every_shipped_day_carries_a_level_for_every_site(phoenix_jobs) -> None:
    days = level_days("phoenix", path=ROOT / DEFAULT_LEVEL_PATH)

    assert len(days) >= 20
    wanted = {job.job_id for job in phoenix_jobs}
    for day in days:
        levels = load_measured_level("phoenix", day, path=ROOT / DEFAULT_LEVEL_PATH)
        assert set(levels) >= wanted, f"{day} is missing a site"
        assert all(20.0 < value < 55.0 for value in levels.values())


def test_a_shipped_day_plans_without_any_network_call(phoenix_jobs) -> None:
    """The judge's case: an empty cache, no credits, and a date long past."""

    from datetime import UTC, datetime

    from certiroute.daily_level import DailyLevelReading

    model = load_climatology("phoenix", root=ROOT / "data" / "climatology")
    graded = [
        day
        for day in level_days("phoenix", path=ROOT / DEFAULT_LEVEL_PATH)
        if day not in set(model.training_dates)
        and day not in set(model.evaluation.holdout_dates)
    ]
    assert graded, "no shipped day sits outside the model's own training window"
    target = max(graded)

    levels = load_measured_level("phoenix", target, path=ROOT / DEFAULT_LEVEL_PATH)
    reading = DailyLevelReading(
        target_date=target,
        granularity_m=model.granularity_m,
        level_by_job={job.job_id: levels[job.job_id] for job in phoenix_jobs},
        activity_id="committed",
        collected_at_utc=datetime.combine(target, time(12), tzinfo=UTC),
        cache_hit=True,
    )

    plan = build_same_day_plan(
        phoenix_jobs,
        model,
        reading,
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=(time(5, 0), time(6, 0), time(7, 0), time(8, 0)),
        shift_end=time(16, 0),
    )

    assert plan.recommended_start in {time(5, 0), time(6, 0), time(7, 0), time(8, 0)}
    assert plan.crew_plan.stops
    assert plan.interval_radius_c > 0


def test_the_committed_levels_are_the_aggregate_not_a_derived_mean() -> None:
    """Deriving the level from the hourly measurements would skew it warm.

    The mean of 05:00-17:00 runs about 0.74 C above the whole-day aggregate,
    and the model's offsets were learned against the aggregate. A level defined
    one way at training and another at serving is the skew this project has
    already been bitten by once, so the levels are carried across rather than
    recomputed.
    """

    from certiroute.measured import load_measured_profiles

    shared = [
        day
        for day in level_days("phoenix", path=ROOT / DEFAULT_LEVEL_PATH)
        if day in set(available_days("phoenix", path=ROOT / DEFAULT_PROFILE_PATH))
    ]
    assert shared

    gaps = []
    for day in shared:
        levels = load_measured_level("phoenix", day, path=ROOT / DEFAULT_LEVEL_PATH)
        profiles = load_measured_profiles(
            "phoenix", day, path=ROOT / DEFAULT_PROFILE_PATH
        )
        hourly_mean = sum(
            point.temperature_c
            for profile in profiles.values()
            for point in profile.points
        ) / sum(len(profile.points) for profile in profiles.values())
        gaps.append(hourly_mean - sum(levels.values()) / len(levels))

    # Consistently warmer, which is exactly why one is not a stand-in for the other.
    assert sum(gaps) / len(gaps) > 0.3
