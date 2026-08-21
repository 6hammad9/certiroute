from datetime import time

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.optimization import (
    ConditionPoint,
    InfeasibleScheduleError,
    ScheduleSearchLimitError,
    ScheduleStrategy,
    TemperatureProfile,
    compare_schedules,
)


def make_profile(job_id: str, morning: float, noon: float, certainty: float = 1.0):
    return TemperatureProfile(
        job_id=job_id,
        points=(
            ConditionPoint(
                minute_of_day=8 * 60,
                temperature_c=morning,
                certainty=certainty,
            ),
            ConditionPoint(
                minute_of_day=13 * 60,
                temperature_c=noon,
                certainty=certainty,
            ),
        ),
    )


def test_temperature_profile_interpolates() -> None:
    profile = make_profile("A", morning=30, noon=40, certainty=0.8)

    temperature, certainty = profile.condition_at(10.5 * 60)

    assert temperature == 35
    assert certainty == 0.8


def test_schedule_comparison_preserves_jobs_and_improves_objectives() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    jobs = [
        Job(
            job_id="C",
            name="Far job",
            location=GeoPoint(latitude=33.465, longitude=-112.055),
            duration_minutes=60,
            earliest_start=time(8, 0),
            latest_finish=time(16, 30),
        ),
        Job(
            job_id="A",
            name="Heat-sensitive job",
            location=GeoPoint(latitude=33.449, longitude=-112.073),
            duration_minutes=90,
            earliest_start=time(8, 0),
            latest_finish=time(16, 30),
        ),
        Job(
            job_id="B",
            name="Uncertain job",
            location=GeoPoint(latitude=33.455, longitude=-112.068),
            duration_minutes=75,
            earliest_start=time(8, 0),
            latest_finish=time(16, 30),
        ),
    ]
    profiles = {
        "A": make_profile("A", morning=28, noon=44, certainty=0.95),
        "B": make_profile("B", morning=30, noon=37, certainty=0.35),
        "C": make_profile("C", morning=31, noon=35, certainty=0.9),
    }

    plans = compare_schedules(jobs, profiles, depot=depot, beam_width=100)

    expected_ids = {"A", "B", "C"}
    for plan in plans.values():
        assert {stop.job_id for stop in plan.stops} == expected_ids
        assert len(plan.stops) == 3

    original = plans[ScheduleStrategy.ORIGINAL]
    efficient = plans[ScheduleStrategy.EFFICIENCY]
    heat_aware = plans[ScheduleStrategy.HEAT_AWARE]
    certainty_aware = plans[ScheduleStrategy.CERTAINTY_AWARE]

    assert efficient.total_travel_minutes <= original.total_travel_minutes
    assert heat_aware.total_raw_exposure_units <= original.total_raw_exposure_units
    assert (
        certainty_aware.total_adjusted_exposure_units
        <= original.total_adjusted_exposure_units
    )


def test_certainty_aware_strategy_can_change_the_job_order() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    jobs = [
        Job(
            job_id="A",
            name="Certain",
            location=GeoPoint(latitude=33.449, longitude=-112.073),
            duration_minutes=90,
        ),
        Job(
            job_id="B",
            name="Uncertain",
            location=GeoPoint(latitude=33.450, longitude=-112.072),
            duration_minutes=90,
        ),
    ]
    profiles = {
        "A": make_profile("A", morning=32, noon=39, certainty=1.0),
        "B": make_profile("B", morning=32, noon=39, certainty=0.1),
    }

    plans = compare_schedules(
        jobs,
        profiles,
        depot=depot,
        uncertainty_penalty=2.0,
        heat_weight=10.0,
        beam_width=20,
    )

    certainty_order = [
        stop.job_id for stop in plans[ScheduleStrategy.CERTAINTY_AWARE].stops
    ]
    assert certainty_order[0] == "B"


def test_priority_weighted_delay_schedules_important_jobs_earlier() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    shared_location = GeoPoint(latitude=33.455, longitude=-112.068)
    jobs = [
        Job(
            job_id="LOW",
            name="Low priority",
            location=shared_location,
            duration_minutes=90,
            priority=1,
        ),
        Job(
            job_id="HIGH",
            name="High priority",
            location=shared_location,
            duration_minutes=90,
            priority=5,
        ),
    ]
    profiles = {
        "LOW": make_profile("LOW", morning=32, noon=39),
        "HIGH": make_profile("HIGH", morning=32, noon=39),
    }

    plans = compare_schedules(jobs, profiles, depot=depot, beam_width=10)

    # Same place, same conditions, same duration: travel and heat cannot
    # separate the two orders, so the priority-weighted delay term must put
    # the high-priority job into the earlier slot for every optimized plan.
    for strategy in (
        ScheduleStrategy.EFFICIENCY,
        ScheduleStrategy.HEAT_AWARE,
        ScheduleStrategy.CERTAINTY_AWARE,
    ):
        assert plans[strategy].stops[0].job_id == "HIGH"
    assert plans[strategy].priority_weighted_delay_minutes > 0


def test_infeasible_day_is_surfaced_instead_of_dropping_work() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    jobs = [
        Job(
            job_id="KEEP",
            name="High priority",
            location=depot,
            duration_minutes=300,
            priority=5,
        ),
        Job(
            job_id="DROP",
            name="Low priority",
            location=depot,
            duration_minutes=300,
            priority=1,
        ),
    ]
    profiles = {
        job.job_id: make_profile(job.job_id, morning=30, noon=36) for job in jobs
    }

    with pytest.raises(InfeasibleScheduleError):
        compare_schedules(jobs, profiles, depot=depot, beam_width=10)


def test_time_window_is_respected_by_every_strategy() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    job = Job(
        job_id="WINDOWED",
        name="Windowed work",
        location=depot,
        duration_minutes=60,
        earliest_start=time(10, 0),
        latest_finish=time(12, 0),
    )

    plans = compare_schedules(
        [job],
        {"WINDOWED": make_profile("WINDOWED", morning=30, noon=36)},
        depot=depot,
    )

    for plan in plans.values():
        assert plan.stops[0].start_minute == 10 * 60
        assert plan.stops[0].finish_minute == 11 * 60


def test_single_job_that_cannot_fit_the_shift_is_infeasible() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    job = Job(
        job_id="TOO-LONG",
        name="Too long",
        location=depot,
        duration_minutes=10 * 60,
    )

    with pytest.raises(InfeasibleScheduleError):
        compare_schedules(
            [job],
            {"TOO-LONG": make_profile("TOO-LONG", morning=30, noon=36)},
            depot=depot,
        )


def test_impossible_high_priority_job_is_not_silently_removed() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    impossible = Job(
        job_id="IMPOSSIBLE",
        name="Impossible window",
        location=depot,
        duration_minutes=60,
        priority=5,
        earliest_start=time(8, 0),
        latest_finish=time(8, 30),
    )
    feasible = Job(
        job_id="FEASIBLE",
        name="Feasible work",
        location=depot,
        duration_minutes=60,
        priority=1,
    )
    jobs = [impossible, feasible]
    profiles = {
        job.job_id: make_profile(job.job_id, morning=30, noon=36) for job in jobs
    }

    with pytest.raises(InfeasibleScheduleError):
        compare_schedules(jobs, profiles, depot=depot)


def test_infeasible_original_order_is_not_mislabeled_as_collective_overload() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    jobs = [
        Job(
            job_id="LATE",
            name="Available later",
            location=depot,
            duration_minutes=60,
            earliest_start=time(12, 0),
        ),
        Job(
            job_id="EARLY",
            name="Must finish early",
            location=depot,
            duration_minutes=60,
            latest_finish=time(11, 0),
        ),
    ]
    profiles = {
        job.job_id: make_profile(job.job_id, morning=30, noon=36) for job in jobs
    }

    with pytest.raises(InfeasibleScheduleError, match="Fixed order"):
        compare_schedules(jobs, profiles, depot=depot)


def test_pruned_feasible_branch_is_reported_as_a_search_limit() -> None:
    depot = GeoPoint(latitude=33.4485, longitude=-112.0740)
    jobs = [
        Job(
            job_id="DEADLINE",
            name="Travel first",
            location=GeoPoint(latitude=33.5485, longitude=-112.0740),
            duration_minutes=60,
            latest_finish=time(10, 0),
        ),
        Job(
            job_id="NEAR",
            name="Near depot",
            location=depot,
            duration_minutes=60,
        ),
    ]
    profiles = {
        job.job_id: make_profile(job.job_id, morning=30, noon=36) for job in jobs
    }

    with pytest.raises(ScheduleSearchLimitError, match="beam width of 1"):
        compare_schedules(jobs, profiles, depot=depot, beam_width=1)

    plans = compare_schedules(jobs, profiles, depot=depot, beam_width=2)
    assert len(plans[ScheduleStrategy.EFFICIENCY].stops) == 2
