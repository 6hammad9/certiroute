"""Plan the day that is actually happening.

This is the path the product runs on. Everything it needs is either already
trained and committed (the area's hour offsets) or is one cheap call away
(today's whole-day aggregate). Nothing here replays a finished day, and
nothing here predicts tomorrow - day-ahead level prediction was measured at
2.27 C mean absolute error on this data, with one day missed by 4.62 C, which
is too loose to put a crew's morning on.

The decision this returns is the shift start. Job ordering is computed too,
but it is reported for what it is: on real Phoenix, Houston and Miami data,
reordering stops inside a fixed window changed the recommended sequence in
zero of three cases, because site-to-site spread (0.32-2.32 C) is far smaller
than the diurnal swing (5.2-9.3 C). Moving the window is the lever that works.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, time

from certiroute.climatology import DiurnalClimatology
from certiroute.daily_level import DailyLevelReading
from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import InsufficientHistoryError
from certiroute.optimization import (
    ConditionPoint,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
    compare_schedules,
)
from certiroute.reliability.calibration import (
    finite_sample_absolute_residual_quantile,
)
from certiroute.shift_timing import (
    DEFAULT_CANDIDATE_STARTS,
    ShiftTimingComparison,
    compare_shift_starts,
)


class PlanningCoverageError(ValueError):
    """The trained model does not reach the hours this shift needs."""


@dataclass(frozen=True)
class SameDayPlan:
    """One day's start-time decision, with the evidence behind it."""

    target_date: date
    area_label: str
    comparison: ShiftTimingComparison
    expected_profiles: dict[str, TemperatureProfile]
    conservative_profiles: dict[str, TemperatureProfile]
    interval_radius_c: float
    coverage: float
    level_reading: DailyLevelReading
    climatology: DiurnalClimatology
    efficient_plan: SchedulePlan
    heat_aware_plan: SchedulePlan

    @property
    def recommended_start(self) -> time:
        return self.comparison.recommended.shift_start

    @property
    def baseline_start(self) -> time:
        return self.comparison.baseline.shift_start

    @property
    def changes_the_start(self) -> bool:
        return self.comparison.changes_the_start

    @property
    def minutes_earlier(self) -> int:
        return self.comparison.minutes_earlier

    @property
    def exposure_reduction(self) -> float | None:
        """Exposure avoided by moving the window, on the conservative curve."""

        return self.comparison.exposure_reduction

    @property
    def reorder_changes_sequence(self) -> bool:
        """Whether heat-aware ordering picked a different visit sequence."""

        return [stop.job_id for stop in self.efficient_plan.stops] != [
            stop.job_id for stop in self.heat_aware_plan.stops
        ]

    @property
    def crew_plan(self) -> SchedulePlan:
        """The plan the crew should follow at the recommended start."""

        recommended = self.comparison.recommended.plan
        if recommended is None:  # pragma: no cover - compare guarantees a plan
            raise ValueError("the recommended option carries no schedule")
        return recommended


def _shift_conservative(
    profiles: Mapping[str, TemperatureProfile], radius_c: float
) -> dict[str, TemperatureProfile]:
    """Raise every predicted point by the calibrated radius.

    Planning against the top of the interval rather than its middle is what
    makes a hotter-than-predicted day still land inside the assumption the
    start time was chosen under.
    """

    return {
        job_id: TemperatureProfile(
            job_id=job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=point.minute_of_day,
                    temperature_c=point.temperature_c + radius_c,
                    certainty=point.certainty,
                )
                for point in profile.points
            ),
        )
        for job_id, profile in profiles.items()
    }


def required_minutes(
    candidate_starts: Sequence[time], shift_end: time
) -> tuple[int, int]:
    """The first and last minute any candidate schedule could touch."""

    if not candidate_starts:
        raise ValueError("at least one candidate start is required")
    starts = [start.hour * 60 + start.minute for start in candidate_starts]
    return min(starts), shift_end.hour * 60 + shift_end.minute


def build_same_day_plan(
    jobs: Sequence[Job],
    climatology: DiurnalClimatology,
    level_reading: DailyLevelReading,
    *,
    depot: GeoPoint,
    baseline_start: time = time(8, 0),
    candidate_starts: Sequence[time] = DEFAULT_CANDIDATE_STARTS,
    shift_end: time = time(17, 0),
    **scheduler_options: object,
) -> SameDayPlan:
    """Turn today's measured level into a start-time decision.

    Nothing in here touches the network; the caller supplies the one reading
    that had to be fetched. That keeps the decision reproducible from stored
    evidence alone.
    """

    if not jobs:
        raise ValueError("at least one job is required")
    missing = sorted(
        job.job_id for job in jobs if job.job_id not in level_reading.level_by_job
    )
    if missing:
        raise ValueError(
            "no whole-day level was read for: " + ", ".join(missing)
        )

    first_minute, last_minute = required_minutes(
        [*candidate_starts, baseline_start], shift_end
    )
    covered = climatology.shape.offsets_by_minute
    if not covered:
        raise PlanningCoverageError("the trained model covers no hours")
    if min(covered) > first_minute or max(covered) < last_minute:
        raise PlanningCoverageError(
            f"{climatology.label} is trained for "
            f"{min(covered) // 60:02d}:{min(covered) % 60:02d}-"
            f"{max(covered) // 60:02d}:{max(covered) % 60:02d}; this shift needs "
            f"{first_minute // 60:02d}:{first_minute % 60:02d}-"
            f"{last_minute // 60:02d}:{last_minute % 60:02d}"
        )

    expected = climatology.predict_profiles(
        {job.job_id: level_reading.level_by_job[job.job_id] for job in jobs}
    )

    evaluation = climatology.evaluation
    miscoverage = evaluation.supported_miscoverage
    if not evaluation.day_scores_c:
        raise InsufficientHistoryError(
            "the trained model carries no calibration scores"
        )
    quantile = finite_sample_absolute_residual_quantile(
        list(evaluation.day_scores_c), miscoverage=miscoverage
    )
    radius = quantile.absolute_residual_quantile_c
    conservative = _shift_conservative(expected, radius)

    comparison = compare_shift_starts(
        list(jobs),
        conservative,
        depot=depot,
        baseline_start=baseline_start,
        candidate_starts=candidate_starts,
        shift_end=shift_end,
        **scheduler_options,  # type: ignore[arg-type]
    )
    plans = compare_schedules(
        list(jobs),
        conservative,
        depot=depot,
        shift_start=comparison.recommended.shift_start,
        shift_end=shift_end,
        **scheduler_options,  # type: ignore[arg-type]
    )
    return SameDayPlan(
        target_date=level_reading.target_date,
        area_label=climatology.label,
        comparison=comparison,
        expected_profiles=expected,
        conservative_profiles=conservative,
        interval_radius_c=radius,
        coverage=1 - miscoverage,
        level_reading=level_reading,
        climatology=climatology,
        efficient_plan=plans[ScheduleStrategy.EFFICIENCY],
        heat_aware_plan=plans[ScheduleStrategy.HEAT_AWARE],
    )


__all__ = [
    "PlanningCoverageError",
    "SameDayPlan",
    "build_same_day_plan",
    "required_minutes",
]
