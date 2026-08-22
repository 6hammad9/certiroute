"""Tests for shift-start comparison, the lever real data actually supports."""

from datetime import time

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.optimization import (
    ConditionPoint,
    InfeasibleScheduleError,
    TemperatureProfile,
)
from certiroute.shift_timing import (
    ProfileCoverageError,
    compare_shift_starts,
    profile_coverage,
)

DEPOT = GeoPoint(latitude=33.4485, longitude=-112.0740)

# Measured FortyGuard values for central Phoenix on 2025-04-15, extended with
# the pre-dawn hours that were collected separately. A real diurnal curve.
MEASURED = {
    5: 16.9, 6: 16.8, 7: 19.7, 8: 21.8, 9: 25.7, 10: 29.6, 11: 32.8,
    12: 33.8, 13: 34.5, 14: 34.6, 15: 34.2, 16: 34.1, 17: 33.8,
}


def profile(job_id: str, readings: dict[int, float]) -> TemperatureProfile:
    return TemperatureProfile(
        job_id=job_id,
        points=tuple(
            ConditionPoint(minute_of_day=hour * 60, temperature_c=value, certainty=1.0)
            for hour, value in sorted(readings.items())
        ),
    )


def make_jobs(count: int = 3, duration: int = 60) -> list[Job]:
    return [
        Job(
            job_id=f"J{index}",
            name=f"Job {index}",
            location=GeoPoint(latitude=33.45 + 0.004 * index, longitude=-112.07),
            duration_minutes=duration,
            priority=3,
        )
        for index in range(1, count + 1)
    ]


@pytest.fixture
def measured_profiles() -> dict[str, TemperatureProfile]:
    return {job.job_id: profile(job.job_id, MEASURED) for job in make_jobs()}


def test_profile_coverage_reports_the_common_measured_window(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    assert profile_coverage(measured_profiles) == (5 * 60, 17 * 60)


def test_earlier_start_lowers_exposure_on_real_measured_temperatures(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    comparison = compare_shift_starts(
        make_jobs(),
        measured_profiles,
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=(time(6, 0), time(8, 0), time(10, 0)),
        shift_end=time(17, 0),
    )

    by_start = {
        option.shift_start: option.exposure_units
        for option in comparison.options
        if option.feasible
    }
    # The curve must be monotonic here: this day warms all morning.
    assert by_start[time(6, 0)] < by_start[time(8, 0)] < by_start[time(10, 0)]
    assert comparison.recommended.shift_start == time(6, 0)
    assert comparison.changes_the_start
    assert comparison.minutes_earlier == 120
    assert comparison.exposure_reduction > 0.30


def test_reduction_is_none_when_the_baseline_has_no_exposure() -> None:
    cool = {hour: 20.0 for hour in range(5, 18)}
    jobs = make_jobs(count=2)
    comparison = compare_shift_starts(
        jobs,
        {job.job_id: profile(job.job_id, cool) for job in jobs},
        depot=DEPOT,
        candidate_starts=(time(6, 0), time(8, 0)),
    )

    assert comparison.exposure_reduction is None


def test_a_start_outside_measured_coverage_is_refused_not_extrapolated(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    # condition_at() clamps to its first sample, so a 04:00 start would silently
    # reuse the 05:00 reading. That must raise instead of quietly misreporting.
    with pytest.raises(ProfileCoverageError, match="precedes measured coverage"):
        compare_shift_starts(
            make_jobs(),
            measured_profiles,
            depot=DEPOT,
            candidate_starts=(time(4, 0), time(8, 0)),
        )


def test_shift_end_beyond_measured_coverage_is_refused(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    with pytest.raises(ProfileCoverageError, match="exceeds"):
        compare_shift_starts(
            make_jobs(),
            measured_profiles,
            depot=DEPOT,
            candidate_starts=(time(8, 0),),
            shift_end=time(19, 0),
        )


def test_infeasible_candidates_are_reported_without_failing_the_sweep(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    # Three 60-minute jobs cannot fit between 16:00 and 17:00.
    comparison = compare_shift_starts(
        make_jobs(),
        measured_profiles,
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=(time(8, 0), time(16, 0)),
        shift_end=time(17, 0),
    )

    late = next(o for o in comparison.options if o.shift_start == time(16, 0))
    assert not late.feasible
    assert late.infeasible_reason
    assert late.exposure_units is None
    assert comparison.recommended.shift_start == time(8, 0)


def test_all_candidates_infeasible_raises(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    with pytest.raises(InfeasibleScheduleError, match="no candidate shift start"):
        compare_shift_starts(
            make_jobs(count=3, duration=120),
            measured_profiles,
            depot=DEPOT,
            baseline_start=time(16, 0),
            candidate_starts=(time(16, 0),),
            shift_end=time(17, 0),
        )


def test_ties_prefer_the_later_start(
    measured_profiles: dict[str, TemperatureProfile],
) -> None:
    """A crew should not be asked to start early without a heat reason."""

    cool = {hour: 20.0 for hour in range(5, 18)}
    jobs = make_jobs(count=2)
    comparison = compare_shift_starts(
        jobs,
        {job.job_id: profile(job.job_id, cool) for job in jobs},
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=(time(6, 0), time(7, 0), time(8, 0)),
    )

    assert comparison.recommended.exposure_units == 0.0
    assert comparison.recommended.shift_start == time(8, 0)
    assert not comparison.changes_the_start
