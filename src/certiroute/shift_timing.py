"""Evaluate *when* a shift should run, not only how its stops are ordered.

Total ambient exposure is the integral of temperature across the minutes a
crew actually works. Reordering jobs inside a fixed window barely moves that
integral, because the jobs still tile the same hours. Moving the window does.

On real FortyGuard data for Phoenix the two levers differ by more than an
order of magnitude, so this module treats the shift start time as a first
class decision and reports the whole exposure curve rather than one answer.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import time

from certiroute.domain import GeoPoint, Job
from certiroute.optimization import (
    InfeasibleScheduleError,
    SchedulePlan,
    ScheduleSearchLimitError,
    ScheduleStrategy,
    TemperatureProfile,
    compare_schedules,
)

DEFAULT_CANDIDATE_STARTS = (
    time(5, 0),
    time(6, 0),
    time(7, 0),
    time(8, 0),
    time(9, 0),
)


class ProfileCoverageError(ValueError):
    """A candidate start falls outside the measured temperature profiles.

    ``TemperatureProfile.condition_at`` clamps to its first sample, so asking
    about 05:00 when measurement begins at 08:00 would silently return the
    08:00 temperature and make an early start look far hotter than it is.
    Refusing is the only honest response.
    """


@dataclass(frozen=True)
class ShiftOption:
    """One candidate shift start and the plan it produces."""

    shift_start: time
    feasible: bool
    plan: SchedulePlan | None = None
    infeasible_reason: str | None = None

    @property
    def start_minute(self) -> int:
        return self.shift_start.hour * 60 + self.shift_start.minute

    @property
    def exposure_units(self) -> float | None:
        return None if self.plan is None else self.plan.total_raw_exposure_units

    @property
    def minutes_above_threshold(self) -> float | None:
        return None if self.plan is None else self.plan.minutes_above_planning_threshold


@dataclass(frozen=True)
class ShiftTimingComparison:
    """Every evaluated start, plus the baseline and the recommendation."""

    options: tuple[ShiftOption, ...]
    baseline: ShiftOption
    recommended: ShiftOption

    @property
    def feasible_options(self) -> tuple[ShiftOption, ...]:
        return tuple(option for option in self.options if option.feasible)

    @property
    def changes_the_start(self) -> bool:
        return self.recommended.shift_start != self.baseline.shift_start

    @property
    def minutes_earlier(self) -> int:
        """How much earlier the recommended start is; negative means later."""

        return self.baseline.start_minute - self.recommended.start_minute

    @property
    def exposure_reduction(self) -> float | None:
        """Fraction of modelled exposure avoided, or None when undefined."""

        baseline_units = self.baseline.exposure_units
        recommended_units = self.recommended.exposure_units
        if baseline_units is None or recommended_units is None:
            return None
        if baseline_units <= 0:
            return None
        return 1 - recommended_units / baseline_units


def profile_coverage(
    profiles: dict[str, TemperatureProfile],
) -> tuple[int, int]:
    """Return the first and last measured minute common to every profile."""

    if not profiles:
        raise ValueError("at least one temperature profile is required")
    earliest = max(profile.points[0].minute_of_day for profile in profiles.values())
    latest = min(profile.points[-1].minute_of_day for profile in profiles.values())
    return earliest, latest


def compare_shift_starts(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    baseline_start: time = time(8, 0),
    candidate_starts: Sequence[time] = DEFAULT_CANDIDATE_STARTS,
    shift_end: time = time(17, 0),
    strategy: ScheduleStrategy = ScheduleStrategy.HEAT_AWARE,
    **scheduler_options: object,
) -> ShiftTimingComparison:
    """Schedule the same jobs from several start times and compare exposure.

    Every candidate is scheduled with identical constraints, so the only
    difference between options is the hour the crew begins. Starts the
    measured profiles cannot cover are rejected rather than extrapolated.
    """

    if not jobs:
        raise ValueError("at least one job is required")
    if not candidate_starts:
        raise ValueError("at least one candidate start is required")

    starts = sorted({*candidate_starts, baseline_start})
    measured_from, measured_to = profile_coverage(profiles)
    earliest_requested = min(start.hour * 60 + start.minute for start in starts)
    if earliest_requested < measured_from:
        raise ProfileCoverageError(
            f"candidate start {earliest_requested // 60:02d}:"
            f"{earliest_requested % 60:02d} precedes measured coverage, which "
            f"begins at {measured_from // 60:02d}:{measured_from % 60:02d}"
        )
    if shift_end.hour * 60 + shift_end.minute > measured_to:
        raise ProfileCoverageError(
            f"shift end {shift_end.isoformat(timespec='minutes')} exceeds "
            f"measured coverage, which ends at "
            f"{measured_to // 60:02d}:{measured_to % 60:02d}"
        )

    options: list[ShiftOption] = []
    for start in starts:
        try:
            plans = compare_schedules(
                jobs,
                profiles,
                depot=depot,
                shift_start=start,
                shift_end=shift_end,
                **scheduler_options,  # type: ignore[arg-type]
            )
        except (InfeasibleScheduleError, ScheduleSearchLimitError) as exc:
            options.append(
                ShiftOption(
                    shift_start=start,
                    feasible=False,
                    infeasible_reason=str(exc),
                )
            )
            continue
        options.append(
            ShiftOption(shift_start=start, feasible=True, plan=plans[strategy])
        )

    ordered = tuple(options)
    baseline = next(
        option for option in ordered if option.shift_start == baseline_start
    )
    feasible = [option for option in ordered if option.feasible]
    if not feasible:
        raise InfeasibleScheduleError(
            "no candidate shift start produces a feasible schedule"
        )

    # Prefer lower exposure; break ties toward the later start so the crew is
    # not asked to begin earlier than the heat actually justifies.
    recommended = min(
        feasible,
        key=lambda option: (option.exposure_units, -option.start_minute),
    )
    return ShiftTimingComparison(
        options=ordered, baseline=baseline, recommended=recommended
    )


__all__ = [
    "DEFAULT_CANDIDATE_STARTS",
    "ProfileCoverageError",
    "ShiftOption",
    "ShiftTimingComparison",
    "compare_shift_starts",
    "profile_coverage",
]
