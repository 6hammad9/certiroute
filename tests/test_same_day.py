"""Tests for the planning path the product actually runs on."""

from datetime import UTC, date, datetime, time

import pytest

from certiroute.climatology import ClimatologyEvaluation, DiurnalClimatology
from certiroute.daily_level import DailyLevelReading
from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import DailyLevelShape
from certiroute.same_day import (
    PlanningCoverageError,
    build_same_day_plan,
    required_minutes,
)

DEPOT = GeoPoint(latitude=33.4485, longitude=-112.0740)
TODAY = date(2026, 8, 22)
CANDIDATES = (time(5, 0), time(6, 0), time(7, 0), time(8, 0))
HOURS = range(5, 18)


@pytest.fixture
def jobs() -> list[Job]:
    return [
        Job(
            job_id=f"J{index}",
            name=f"Job {index}",
            location=GeoPoint(latitude=33.45 + 0.003 * index, longitude=-112.07),
            duration_minutes=60,
            priority=3,
        )
        for index in range(1, 4)
    ]


def climatology(
    *,
    day_scores=(0.4, 0.6, 0.9),
    hours=HOURS,
    step: float = 1.6,
) -> DiurnalClimatology:
    """A model whose hours climb steadily, so morning is genuinely cooler."""

    offsets = {hour * 60: step * (hour - 11) for hour in hours}
    return DiurnalClimatology(
        area_id="phoenix",
        label="Phoenix, Arizona",
        granularity_m=60,
        shape=DailyLevelShape(
            offsets_by_minute=offsets,
            sample_counts=dict.fromkeys(offsets, 12),
            day_count=6,
        ),
        training_dates=(date(2026, 8, 10), date(2026, 8, 11)),
        evaluation=ClimatologyEvaluation(
            holdout_dates=(date(2026, 8, 19),),
            mean_absolute_error_c=0.5,
            worst_absolute_error_c=0.9,
            reading_count=39,
            day_scores_c=tuple(day_scores),
            unseen_site_mae_c=0.55,
        ),
        trained_at_utc=datetime(2026, 8, 22, tzinfo=UTC),
    )


def reading(jobs, level: float = 38.0, target_date: date = TODAY) -> DailyLevelReading:
    return DailyLevelReading(
        target_date=target_date,
        granularity_m=60,
        level_by_job={job.job_id: level for job in jobs},
        activity_id="activity-1",
        collected_at_utc=datetime(2026, 8, 22, 13, tzinfo=UTC),
        cache_hit=False,
    )


def test_required_minutes_spans_the_earliest_start_to_the_shift_end() -> None:
    assert required_minutes(CANDIDATES, time(17, 0)) == (5 * 60, 17 * 60)


def test_required_minutes_needs_a_candidate() -> None:
    with pytest.raises(ValueError, match="at least one candidate start"):
        required_minutes([], time(17, 0))


def test_plan_recommends_the_cooler_early_start(jobs) -> None:
    plan = build_same_day_plan(
        jobs,
        climatology(),
        reading(jobs),
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=CANDIDATES,
    )

    assert plan.recommended_start == time(5, 0)
    assert plan.changes_the_start
    assert plan.minutes_earlier == 180
    assert plan.exposure_reduction is not None and plan.exposure_reduction > 0.3
    assert plan.target_date == TODAY
    assert plan.area_label == "Phoenix, Arizona"


def test_planning_uses_the_conservative_upper_curve(jobs) -> None:
    plan = build_same_day_plan(
        jobs,
        climatology(),
        reading(jobs),
        depot=DEPOT,
        candidate_starts=CANDIDATES,
    )

    expected, _ = plan.expected_profiles["J1"].condition_at(10 * 60)
    cautious, _ = plan.conservative_profiles["J1"].condition_at(10 * 60)
    assert plan.interval_radius_c > 0
    assert cautious == pytest.approx(expected + plan.interval_radius_c)
    # Three held-out days can only support a 75% interval, and the plan says so.
    assert plan.coverage == pytest.approx(0.75)


def test_each_site_keeps_its_own_level(jobs) -> None:
    levels = {"J1": 40.0, "J2": 36.0, "J3": 38.0}
    hot_and_cool = DailyLevelReading(
        target_date=TODAY,
        granularity_m=60,
        level_by_job=levels,
        activity_id="activity-1",
        collected_at_utc=datetime(2026, 8, 22, 13, tzinfo=UTC),
        cache_hit=True,
    )

    plan = build_same_day_plan(
        jobs, climatology(), hot_and_cool, depot=DEPOT, candidate_starts=CANDIDATES
    )

    hot, _ = plan.expected_profiles["J1"].condition_at(11 * 60)
    cool, _ = plan.expected_profiles["J2"].condition_at(11 * 60)
    assert hot - cool == pytest.approx(4.0)


def test_a_site_without_a_level_is_refused_rather_than_filled_in(jobs) -> None:
    partial = DailyLevelReading(
        target_date=TODAY,
        granularity_m=60,
        level_by_job={"J1": 38.0},
        activity_id="activity-1",
        collected_at_utc=datetime(2026, 8, 22, 13, tzinfo=UTC),
        cache_hit=False,
    )

    with pytest.raises(ValueError, match="no whole-day level was read for: J2, J3"):
        build_same_day_plan(
            jobs, climatology(), partial, depot=DEPOT, candidate_starts=CANDIDATES
        )


def test_a_shift_outside_the_trained_hours_is_refused(jobs) -> None:
    """Extrapolating past the trained window would invent a temperature."""

    trained_late = climatology(hours=range(8, 18))

    with pytest.raises(PlanningCoverageError, match="is trained for 08:00-17:00"):
        build_same_day_plan(
            jobs,
            trained_late,
            reading(jobs),
            depot=DEPOT,
            candidate_starts=CANDIDATES,
        )


def test_no_jobs_is_refused(jobs) -> None:
    with pytest.raises(ValueError, match="at least one job"):
        build_same_day_plan(
            [], climatology(), reading(jobs), depot=DEPOT, candidate_starts=CANDIDATES
        )


def test_plan_reports_whether_reordering_changed_the_sequence(jobs) -> None:
    """The reorder result is surfaced as a fact, not assumed to be a win."""

    plan = build_same_day_plan(
        jobs, climatology(), reading(jobs), depot=DEPOT, candidate_starts=CANDIDATES
    )

    assert isinstance(plan.reorder_changes_sequence, bool)
    assert [stop.job_id for stop in plan.crew_plan.stops]
    assert len(plan.crew_plan.stops) == len(jobs)


def test_a_flat_day_leaves_the_start_alone(jobs) -> None:
    """With no diurnal swing there is no heat reason to start earlier."""

    flat = climatology(step=0.0)

    plan = build_same_day_plan(
        jobs, flat, reading(jobs), depot=DEPOT,
        baseline_start=time(8, 0), candidate_starts=CANDIDATES,
    )

    assert plan.recommended_start == time(8, 0)
    assert not plan.changes_the_start
