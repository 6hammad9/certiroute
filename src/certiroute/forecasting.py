"""Predict a day's temperature curve from this vendor's own past records.

FortyGuard exposes no hourly forecast through its heatmap API, so a
forward-looking product has to build its own. Measured on real Phoenix data,
two approaches differ sharply:

* Averaging the same site-hour across recent days gives 1.08 C mean absolute
  error, and almost all of it is bias - every hour of the held-out day was
  under-predicted by roughly the same amount.
* Keeping only the *shape* of those days and setting the level from a
  temperature actually observed on the target morning halves that to 0.52 C.

The second method is what this module implements. Because the predictions are
ours rather than the vendor's, their residuals are legitimately measurable,
which is what makes honest interval calibration possible at all.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from certiroute.optimization import ConditionPoint, TemperatureProfile
from certiroute.reliability.calibration import (
    finite_sample_absolute_residual_quantile,
)

# A profile point carries a certainty field the optimiser requires. Predicted
# profiles use 1.0 so no hidden penalty is applied; uncertainty is expressed
# explicitly through the calibrated interval instead.
NEUTRAL_CERTAINTY = 1.0


class InsufficientHistoryError(ValueError):
    """Too few historical days cover the hours being predicted."""


@dataclass(frozen=True)
class DiurnalShape:
    """How far each hour typically sits above an anchor hour.

    Learned per hour across whole days and sites. Holding the shape while
    letting the day set its own level is what removes the systematic bias.
    """

    anchor_minute: int
    offsets_by_minute: dict[int, float]
    sample_counts: dict[int, int]
    day_count: int

    @property
    def covered_minutes(self) -> tuple[int, ...]:
        return tuple(sorted(self.offsets_by_minute))

    def offset_at(self, minute_of_day: int) -> float:
        try:
            return self.offsets_by_minute[minute_of_day]
        except KeyError as exc:
            raise InsufficientHistoryError(
                f"no learned offset for minute {minute_of_day}"
            ) from exc


@dataclass(frozen=True)
class CalibratedForecast:
    """A predicted curve with a distribution-free interval radius.

    ``radius_c`` is the split-conformal absolute-residual quantile, so
    ``expected +/- radius`` covers the realisation with at least
    ``1 - miscoverage`` probability under exchangeability.
    """

    expected_by_minute: dict[int, float]
    radius_c: float
    miscoverage: float
    calibration_sample_count: int

    def upper_by_minute(self) -> dict[int, float]:
        """The conservative curve a heat plan should be built against."""

        return {
            minute: value + self.radius_c
            for minute, value in self.expected_by_minute.items()
        }

    def to_profile(self, job_id: str, *, conservative: bool = True):
        """Render as a TemperatureProfile the scheduler can consume."""

        source = (
            self.upper_by_minute() if conservative else self.expected_by_minute
        )
        return TemperatureProfile(
            job_id=job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=minute,
                    temperature_c=value,
                    certainty=NEUTRAL_CERTAINTY,
                )
                for minute, value in sorted(source.items())
            ),
        )


@dataclass(frozen=True)
class DailyLevelShape:
    """Hour offsets measured against a whole-day level, not against an hour.

    FortyGuard returns no hourly value for the current date, but it does return
    a whole-day aggregate for it. Anchoring on that aggregate is therefore the
    only way to condition a forecast on same-day information at all, which is
    what separates a planning tool from a replay of the past.

    Measured on real Phoenix days the offsets are highly stable - 08:00 sat
    1.56 and 1.55 C below the aggregate on consecutive days - which is why this
    anchor works despite being a single scalar for the whole day.
    """

    offsets_by_minute: dict[int, float]
    sample_counts: dict[int, int]
    day_count: int

    @property
    def covered_minutes(self) -> tuple[int, ...]:
        return tuple(sorted(self.offsets_by_minute))

    def offset_at(self, minute_of_day: int) -> float:
        try:
            return self.offsets_by_minute[minute_of_day]
        except KeyError as exc:
            raise InsufficientHistoryError(
                f"no learned offset for minute {minute_of_day}"
            ) from exc

    def predict(self, daily_level_c: float) -> dict[int, float]:
        """Apply the learned offsets to a day's aggregate level."""

        if not self.offsets_by_minute:
            raise InsufficientHistoryError("shape covers no minutes")
        return {
            minute: daily_level_c + offset
            for minute, offset in sorted(self.offsets_by_minute.items())
        }


def learn_daily_level_shape(
    history: Sequence[tuple[float, Mapping[str, TemperatureProfile]]],
) -> DailyLevelShape:
    """Learn each hour's offset from its day's aggregate level.

    ``history`` pairs a day's whole-day aggregate with that day's measured
    hourly profiles.
    """

    if not history:
        raise InsufficientHistoryError("at least one historical day is required")

    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    used_days = 0
    for level, day in history:
        contributed = False
        for profile in day.values():
            for minute, value in _readings(profile).items():
                totals[minute] = totals.get(minute, 0.0) + (value - level)
                counts[minute] = counts.get(minute, 0) + 1
                contributed = True
        if contributed:
            used_days += 1

    if not totals:
        raise InsufficientHistoryError("no historical profile contained readings")
    return DailyLevelShape(
        offsets_by_minute={m: totals[m] / counts[m] for m in totals},
        sample_counts=dict(counts),
        day_count=used_days,
    )


def daily_level_residuals(
    shape: DailyLevelShape,
    held_out: Sequence[tuple[float, Mapping[str, TemperatureProfile]]],
) -> list[float]:
    """Signed errors on days the offsets were not learned from."""

    residuals: list[float] = []
    for level, day in held_out:
        predicted = shape.predict(level)
        for profile in day.values():
            for minute, actual in _readings(profile).items():
                if minute in predicted:
                    residuals.append(predicted[minute] - actual)
    return residuals


def _readings(profile: TemperatureProfile) -> dict[int, float]:
    return {point.minute_of_day: point.temperature_c for point in profile.points}


def learn_diurnal_shape(
    history: Sequence[Mapping[str, TemperatureProfile]],
    *,
    anchor_minute: int,
) -> DiurnalShape:
    """Average each hour's offset from the anchor across historical days.

    ``history`` is one mapping of job id to measured profile per day. Days that
    lack the anchor hour contribute nothing, because without it there is no
    offset to measure.
    """

    if not history:
        raise InsufficientHistoryError("at least one historical day is required")

    totals: dict[int, float] = {}
    counts: dict[int, int] = {}
    contributing_days = 0
    for day in history:
        day_contributed = False
        for profile in day.values():
            readings = _readings(profile)
            anchor = readings.get(anchor_minute)
            if anchor is None:
                continue
            for minute, value in readings.items():
                totals[minute] = totals.get(minute, 0.0) + (value - anchor)
                counts[minute] = counts.get(minute, 0) + 1
                day_contributed = True
        if day_contributed:
            contributing_days += 1

    if not totals:
        raise InsufficientHistoryError(
            f"no historical profile contained the anchor minute {anchor_minute}"
        )
    return DiurnalShape(
        anchor_minute=anchor_minute,
        offsets_by_minute={
            minute: totals[minute] / counts[minute] for minute in totals
        },
        sample_counts=dict(counts),
        day_count=contributing_days,
    )


def predict_from_anchor(
    shape: DiurnalShape,
    anchor_temperature_c: float,
    *,
    minutes: Sequence[int] | None = None,
) -> dict[int, float]:
    """Apply the learned shape to a level observed on the target day."""

    wanted = tuple(minutes) if minutes is not None else shape.covered_minutes
    if not wanted:
        raise InsufficientHistoryError("no minutes requested or covered")
    return {
        minute: anchor_temperature_c + shape.offset_at(minute)
        for minute in sorted(wanted)
    }


def shape_residuals(
    shape: DiurnalShape,
    held_out: Sequence[Mapping[str, TemperatureProfile]],
    *,
    exclude_anchor: bool = True,
) -> list[float]:
    """Signed prediction errors on days the shape was not learned from.

    These must come from held-out days: reusing training days would understate
    the residuals and inflate the resulting coverage.
    """

    residuals: list[float] = []
    for day in held_out:
        for profile in day.values():
            readings = _readings(profile)
            anchor = readings.get(shape.anchor_minute)
            if anchor is None:
                continue
            for minute, actual in readings.items():
                if exclude_anchor and minute == shape.anchor_minute:
                    continue
                if minute not in shape.offsets_by_minute:
                    continue
                residuals.append((anchor + shape.offset_at(minute)) - actual)
    return residuals


def day_blocked_residual_scores(
    shape: DiurnalShape,
    held_out: Sequence[Mapping[str, TemperatureProfile]],
    *,
    exclude_anchor: bool = True,
) -> list[float]:
    """One conformity score per held-out day: its largest absolute error.

    Errors within a single day are strongly correlated - a day simply runs hot
    or cool - so pooling every site-hour pretends to far more independent
    evidence than exists. Treating the day as the exchangeable unit fixes the
    effective sample size at the day count, and the resulting radius covers a
    whole day's curve simultaneously rather than one hour pointwise.
    """

    scores: list[float] = []
    for day in held_out:
        residuals = shape_residuals(
            shape, [day], exclude_anchor=exclude_anchor
        )
        if residuals:
            scores.append(max(abs(value) for value in residuals))
    if not scores:
        raise InsufficientHistoryError(
            "no held-out day produced a residual score"
        )
    return scores


def calibrate_forecast(
    shape: DiurnalShape,
    anchor_temperature_c: float,
    calibration_residuals_c: Sequence[float],
    *,
    miscoverage: float = 0.1,
    minutes: Sequence[int] | None = None,
) -> CalibratedForecast:
    """Predict a curve and attach a split-conformal interval radius."""

    expected = predict_from_anchor(
        shape, anchor_temperature_c, minutes=minutes
    )
    quantile = finite_sample_absolute_residual_quantile(
        [abs(value) for value in calibration_residuals_c],
        miscoverage=miscoverage,
    )
    return CalibratedForecast(
        expected_by_minute=expected,
        radius_c=quantile.absolute_residual_quantile_c,
        miscoverage=miscoverage,
        calibration_sample_count=quantile.sample_count,
    )


def empirical_coverage(
    residuals_c: Sequence[float], radius_c: float
) -> float:
    """Fraction of residuals the interval actually captured."""

    if not residuals_c:
        raise ValueError("coverage needs at least one residual")
    if radius_c < 0:
        raise ValueError("radius cannot be negative")
    covered = sum(1 for value in residuals_c if abs(value) <= radius_c)
    return covered / len(residuals_c)


__all__ = [
    "NEUTRAL_CERTAINTY",
    "CalibratedForecast",
    "DailyLevelShape",
    "DiurnalShape",
    "InsufficientHistoryError",
    "calibrate_forecast",
    "daily_level_residuals",
    "day_blocked_residual_scores",
    "empirical_coverage",
    "learn_daily_level_shape",
    "learn_diurnal_shape",
    "predict_from_anchor",
    "shape_residuals",
]
