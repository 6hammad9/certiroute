"""Turn a forecast into a shift-start decision, then check it against reality.

This closes the loop the project needs: learn a diurnal shape from past days,
anchor it to a temperature observed on the target morning, widen it by a
day-blocked conformal radius, choose the shift start that minimises exposure
on that conservative curve, and finally score the choice against the day's
realised temperatures.

The scoring step is the point. A recommendation that only looks good on its
own forecast proves nothing; the question is whether the hours it picked were
actually cooler once the day happened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import time

from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import (
    CalibratedForecast,
    DiurnalShape,
    calibrate_forecast,
)
from certiroute.optimization import TemperatureProfile
from certiroute.shift_timing import (
    DEFAULT_CANDIDATE_STARTS,
    ShiftTimingComparison,
    compare_shift_starts,
)


@dataclass(frozen=True)
class ShiftRecommendation:
    """A start time chosen on the conservative predicted curve."""

    recommended_start: time
    baseline_start: time
    comparison: ShiftTimingComparison
    forecast: CalibratedForecast
    predicted_profiles: dict[str, TemperatureProfile]

    @property
    def changes_the_start(self) -> bool:
        return self.recommended_start != self.baseline_start

    @property
    def minutes_earlier(self) -> int:
        baseline = self.baseline_start.hour * 60 + self.baseline_start.minute
        recommended = (
            self.recommended_start.hour * 60 + self.recommended_start.minute
        )
        return baseline - recommended


@dataclass(frozen=True)
class RecommendationOutcome:
    """What the recommended start actually cost once the day happened."""

    recommended_start: time
    baseline_start: time
    realized_recommended_units: float
    realized_baseline_units: float
    realized_best_start: time
    realized_best_units: float

    @property
    def realized_reduction(self) -> float | None:
        """Exposure avoided against the baseline, on realised temperatures."""

        if self.realized_baseline_units <= 0:
            return None
        return 1 - self.realized_recommended_units / self.realized_baseline_units

    @property
    def helped(self) -> bool:
        return self.realized_recommended_units < self.realized_baseline_units

    @property
    def chose_the_best_start(self) -> bool:
        return self.recommended_start == self.realized_best_start

    @property
    def regret_units(self) -> float:
        """Exposure above the best start that hindsight would have picked."""

        return self.realized_recommended_units - self.realized_best_units


def recommend_shift_start(
    jobs: Sequence[Job],
    shape: DiurnalShape,
    anchor_temperature_c: float,
    calibration_scores_c: Sequence[float],
    *,
    depot: GeoPoint,
    baseline_start: time = time(8, 0),
    candidate_starts: Sequence[time] = DEFAULT_CANDIDATE_STARTS,
    shift_end: time = time(17, 0),
    miscoverage: float = 0.1,
    conservative: bool = True,
    **scheduler_options: object,
) -> ShiftRecommendation:
    """Choose a start time on the calibrated upper bound of the forecast.

    Planning against the upper curve rather than the expectation is what makes
    the choice cautious: a day that runs hotter than predicted still lands
    inside the interval the start time was selected under.
    """

    forecast = calibrate_forecast(
        shape,
        anchor_temperature_c,
        calibration_scores_c,
        miscoverage=miscoverage,
    )
    predicted = {
        job.job_id: forecast.to_profile(job.job_id, conservative=conservative)
        for job in jobs
    }
    comparison = compare_shift_starts(
        list(jobs),
        predicted,
        depot=depot,
        baseline_start=baseline_start,
        candidate_starts=candidate_starts,
        shift_end=shift_end,
        **scheduler_options,  # type: ignore[arg-type]
    )
    return ShiftRecommendation(
        recommended_start=comparison.recommended.shift_start,
        baseline_start=baseline_start,
        comparison=comparison,
        forecast=forecast,
        predicted_profiles=predicted,
    )


def score_against_realization(
    recommendation: ShiftRecommendation,
    jobs: Sequence[Job],
    realized_profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    candidate_starts: Sequence[time] = DEFAULT_CANDIDATE_STARTS,
    shift_end: time = time(17, 0),
    **scheduler_options: object,
) -> RecommendationOutcome:
    """Replay every candidate start on the day's measured temperatures.

    The recommendation is never allowed to see these profiles; they are only
    used to score a decision that was already made.
    """

    realized = compare_shift_starts(
        list(jobs),
        dict(realized_profiles),
        depot=depot,
        baseline_start=recommendation.baseline_start,
        candidate_starts=candidate_starts,
        shift_end=shift_end,
        **scheduler_options,  # type: ignore[arg-type]
    )
    by_start = {
        option.shift_start: option.exposure_units
        for option in realized.options
        if option.feasible and option.exposure_units is not None
    }
    if recommendation.recommended_start not in by_start:
        raise ValueError(
            "the recommended start is infeasible on the realised day, so it "
            "cannot be scored"
        )
    if recommendation.baseline_start not in by_start:
        raise ValueError("the baseline start is infeasible on the realised day")

    best_start = min(by_start, key=lambda start: (by_start[start], -(
        start.hour * 60 + start.minute
    )))
    return RecommendationOutcome(
        recommended_start=recommendation.recommended_start,
        baseline_start=recommendation.baseline_start,
        realized_recommended_units=by_start[recommendation.recommended_start],
        realized_baseline_units=by_start[recommendation.baseline_start],
        realized_best_start=best_start,
        realized_best_units=by_start[best_start],
    )


__all__ = [
    "RecommendationOutcome",
    "ShiftRecommendation",
    "recommend_shift_start",
    "score_against_realization",
]
