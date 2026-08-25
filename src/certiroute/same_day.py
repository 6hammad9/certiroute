"""Plan the day that is actually happening.

This is the path the product runs on. Everything it needs is either already
trained and committed (the area's hour offsets) or is one cheap call away
(the whole-day aggregate). Nothing here replays a finished day.

Tomorrow can be planned, one day out, anchored on today's reading - the call a
dispatcher actually makes the evening before. That is narrower than it sounds.
Day-ahead *temperature* is poor: predicting tomorrow's level measured 2.27 C
mean absolute error with one day missed by 4.62 C. But the output here is a
start time, and an error in the level shifts the whole curve without reordering
its hours. Measured across Phoenix, Houston and Miami, the evening-before plan
chose the same start as the morning-of plan on every day tested, including days
the level was out by more than 3 C. So the timing is offered and the
temperatures are widened to the day-ahead calibration, which is the honest
split between what survives a day and what does not.

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

from certiroute.climatology import (
    DEFAULT_TRAINED_RADIUS_KM,
    DiurnalClimatology,
    OutsideTrainedAreaError,
)
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
from certiroute.shift_planning import (
    RecommendationOutcome,
    score_starts_against_realization,
)
from certiroute.shift_timing import (
    DEFAULT_CANDIDATE_STARTS,
    ShiftTimingComparison,
    compare_shift_starts,
)

# A recommendation to leave right now is not actionable: a crew has to be
# told, and has to get to the base. Anything sooner than this is treated as
# already gone.
DEFAULT_LEAD_MINUTES = 30


# Day-ahead scores come from anchoring each day on the one directly before
# it, so they describe a one-day gap and nothing longer.
MAX_LEAD_DAYS = 1


class PlanningLeadError(ValueError):
    """The plan reaches further ahead than the model was calibrated for."""


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
    # Days between the reading that anchored this plan and the day it plans.
    # One means the evening before: same decision, wider interval, and
    # temperatures that are indicative rather than measured.
    lead_days: int
    level_reading: DailyLevelReading
    climatology: DiurnalClimatology
    efficient_plan: SchedulePlan
    heat_aware_plan: SchedulePlan
    windows: WindowRelaxation
    # Where the shift begins and ends. Carried so anything rendering the
    # plan can draw the whole round trip without being handed it again.
    depot: GeoPoint

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


@dataclass(frozen=True)
class WindowRelaxation:
    """Which job windows moved with the shift, and which are real constraints."""

    jobs: tuple[Job, ...]
    moved_job_ids: tuple[str, ...]
    held_job_ids: tuple[str, ...]

    @property
    def earliest_held_start(self) -> time | None:
        """The site constraint that binds an earlier shift, if any."""

        held = [
            job.earliest_start
            for job in self.jobs
            if job.job_id in set(self.held_job_ids) and job.earliest_start is not None
        ]
        return min(held) if held else None


def relax_windows_to(
    jobs: Sequence[Job], *, baseline_start: time, earliest_start: time
) -> WindowRelaxation:
    """Let job windows follow the shift when they were only pinned to it.

    A job whose window opens no later than the crew's usual start was never
    constrained by the site - it inherited the shift. Moving the shift earlier
    has to move those windows too, or every earlier start is silently
    infeasible and the recommendation quietly collapses back to the status quo.

    A window that opens *after* the usual start is a genuine site constraint -
    a gate that unlocks at nine, a tenant who must be present - and is left
    exactly as it is.
    """

    moved: list[str] = []
    held: list[str] = []
    relaxed: list[Job] = []
    for job in jobs:
        current = job.earliest_start
        if current is not None and current > baseline_start:
            held.append(job.job_id)
            relaxed.append(job)
            continue
        if current is None or current <= earliest_start:
            relaxed.append(job)
            continue
        moved.append(job.job_id)
        relaxed.append(job.model_copy(update={"earliest_start": earliest_start}))
    return WindowRelaxation(
        jobs=tuple(relaxed),
        moved_job_ids=tuple(moved),
        held_job_ids=tuple(held),
    )


def _plus_minutes(value: time, minutes: int) -> time:
    """Advance a wall clock, clamped to the end of the day rather than wrapped.

    Wrapping past midnight would turn "too late to start" into "very early
    tomorrow", which is exactly the wrong answer.
    """

    total = min(value.hour * 60 + value.minute + minutes, 24 * 60 - 1)
    return time(total // 60, total % 60)


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
    trained_radius_km: float = DEFAULT_TRAINED_RADIUS_KM,
    now: time | None = None,
    lead_minutes: int = DEFAULT_LEAD_MINUTES,
    target_date: date | None = None,
    **scheduler_options: object,
) -> SameDayPlan:
    """Turn today's measured level into a start-time decision.

    Nothing in here touches the network; the caller supplies the one reading
    that had to be fetched. That keeps the decision reproducible from stored
    evidence alone.

    ``now`` is the wall clock the plan has to be actionable against. Without
    it the coolest hour is usually one that has already gone, and a crew
    cannot be sent into the morning from the afternoon. Leave it unset when
    replaying a finished day.

    ``target_date`` plans a day later than the reading anchoring it - the
    evening-before case. Measured across three cities, that picked the same
    start as the morning-of plan on every day tested, because an error in the
    day's level shifts the whole curve without reordering its hours. The
    interval widens to the day-ahead calibration, because the temperatures
    themselves are a further day's guess.
    """

    if not jobs:
        raise ValueError("at least one job is required")
    missing = sorted(
        job.job_id for job in jobs if job.job_id not in level_reading.level_by_job
    )
    if missing:
        raise ValueError("no whole-day level was read for: " + ", ".join(missing))

    far = climatology.sites_outside_trained_area(
        {job.job_id: (job.location.latitude, job.location.longitude) for job in jobs},
        radius_km=trained_radius_km,
    )
    if far:
        worst = max(far.values())
        raise OutsideTrainedAreaError(
            f"{len(far)} work site(s) lie up to {worst:.0f} km from the ground "
            f"{climatology.label} was trained on, which is beyond the "
            f"{trained_radius_km:.0f} km this model is valid for: "
            + ", ".join(sorted(far))
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

    planned_for = target_date or level_reading.target_date
    lead_days = max((planned_for - level_reading.target_date).days, 0)
    if lead_days > MAX_LEAD_DAYS:
        # The day-ahead scores were produced by anchoring each day on the one
        # directly before it. Reusing them across a longer gap would claim a
        # confidence that was never measured at that distance.
        raise PlanningLeadError(
            f"this model is calibrated {MAX_LEAD_DAYS} day ahead, and "
            f"{planned_for.isoformat()} is {lead_days} days past the reading "
            "anchoring it"
        )

    evaluation = climatology.evaluation
    if lead_days:
        if not evaluation.day_ahead_scores_c:
            raise InsufficientHistoryError(
                "this model has no day-ahead calibration, so a plan made the "
                "evening before cannot state an honest interval"
            )
        scores = list(evaluation.day_ahead_scores_c)
        miscoverage = evaluation.day_ahead_miscoverage
    else:
        if not evaluation.day_scores_c:
            raise InsufficientHistoryError(
                "the trained model carries no calibration scores"
            )
        scores = list(evaluation.day_scores_c)
        miscoverage = evaluation.supported_miscoverage
    quantile = finite_sample_absolute_residual_quantile(scores, miscoverage=miscoverage)
    radius = quantile.absolute_residual_quantile_c
    conservative = _shift_conservative(expected, radius)

    # Windows that merely inherited the usual shift have to follow it, or an
    # earlier start is infeasible for reasons that have nothing to do with heat.
    windows = relax_windows_to(
        jobs,
        baseline_start=baseline_start,
        earliest_start=min([*candidate_starts, baseline_start]),
    )
    schedulable = list(windows.jobs)

    not_before = None if now is None else _plus_minutes(now, lead_minutes)
    comparison = compare_shift_starts(
        schedulable,
        conservative,
        depot=depot,
        baseline_start=baseline_start,
        candidate_starts=candidate_starts,
        shift_end=shift_end,
        not_before=not_before,
        **scheduler_options,  # type: ignore[arg-type]
    )
    plans = compare_schedules(
        schedulable,
        conservative,
        depot=depot,
        shift_start=comparison.recommended.shift_start,
        shift_end=shift_end,
        **scheduler_options,  # type: ignore[arg-type]
    )
    return SameDayPlan(
        target_date=planned_for,
        area_label=climatology.label,
        comparison=comparison,
        expected_profiles=expected,
        conservative_profiles=conservative,
        interval_radius_c=radius,
        coverage=1 - miscoverage,
        lead_days=lead_days,
        level_reading=level_reading,
        climatology=climatology,
        efficient_plan=plans[ScheduleStrategy.EFFICIENCY],
        heat_aware_plan=plans[ScheduleStrategy.HEAT_AWARE],
        windows=windows,
        depot=depot,
    )


class LeakageError(ValueError):
    """A day cannot score a model that already learned from it."""


def score_plan_against_measurements(
    plan: SameDayPlan,
    realized_profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    candidate_starts: Sequence[time] = DEFAULT_CANDIDATE_STARTS,
    shift_end: time = time(17, 0),
    **scheduler_options: object,
) -> RecommendationOutcome:
    """Check a start-time decision against the temperatures the day produced.

    This is the only honest way to answer whether the recommendation works, so
    it refuses on any day the model trained on. Scoring a training day would
    measure memory, not skill, and would produce exactly the flattering number
    that makes a system untrustworthy.
    """

    seen = set(plan.climatology.training_dates) | set(
        plan.climatology.evaluation.holdout_dates
    )
    if plan.target_date in seen:
        role = (
            "trained on"
            if plan.target_date in set(plan.climatology.training_dates)
            else "calibrated its interval on"
        )
        raise LeakageError(
            f"{plan.target_date.isoformat()} is a day this model {role}, so it "
            "cannot be used to score the model"
        )
    return score_starts_against_realization(
        list(plan.windows.jobs),
        realized_profiles,
        recommended_start=plan.recommended_start,
        baseline_start=plan.baseline_start,
        depot=depot,
        candidate_starts=candidate_starts,
        shift_end=shift_end,
        **scheduler_options,
    )


__all__ = [
    "DEFAULT_LEAD_MINUTES",
    "MAX_LEAD_DAYS",
    "LeakageError",
    "PlanningLeadError",
    "PlanningCoverageError",
    "SameDayPlan",
    "WindowRelaxation",
    "build_same_day_plan",
    "relax_windows_to",
    "required_minutes",
    "score_plan_against_measurements",
]
