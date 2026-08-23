"""Tests for building our own forecast from the vendor's past records."""

import pytest

from certiroute.forecasting import (
    InsufficientHistoryError,
    calibrate_forecast,
    empirical_coverage,
    learn_diurnal_shape,
    predict_from_anchor,
    shape_residuals,
)
from certiroute.optimization import ConditionPoint, TemperatureProfile

ANCHOR = 8 * 60


def day(readings: dict[int, float], job_ids=("A", "B")) -> dict:
    """One day of measured profiles sharing the same curve."""

    return {
        job_id: TemperatureProfile(
            job_id=job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=minute, temperature_c=value, certainty=1.0
                )
                for minute, value in sorted(readings.items())
            ),
        )
        for job_id in job_ids
    }


def ramp(start: float, step: float, hours=range(8, 13)) -> dict[int, float]:
    return {hour * 60: start + step * (hour - 8) for hour in hours}


def test_shape_is_the_offset_from_the_anchor_hour() -> None:
    history = [day(ramp(30.0, 2.0)), day(ramp(34.0, 2.0))]

    shape = learn_diurnal_shape(history, anchor_minute=ANCHOR)

    # Both days rise 2 C per hour, so offsets are identical despite the
    # 4 C difference in level. That separation is the whole point.
    assert shape.offset_at(ANCHOR) == pytest.approx(0.0)
    assert shape.offset_at(9 * 60) == pytest.approx(2.0)
    assert shape.offset_at(12 * 60) == pytest.approx(8.0)
    assert shape.day_count == 2


def test_prediction_sets_level_from_the_observed_anchor() -> None:
    shape = learn_diurnal_shape(
        [day(ramp(30.0, 2.0)), day(ramp(34.0, 2.0))], anchor_minute=ANCHOR
    )

    predicted = predict_from_anchor(shape, 40.0)

    assert predicted[ANCHOR] == pytest.approx(40.0)
    assert predicted[10 * 60] == pytest.approx(44.0)


def test_shape_ignores_days_missing_the_anchor_hour() -> None:
    usable = day(ramp(30.0, 2.0))
    no_anchor = day({9 * 60: 33.0, 10 * 60: 35.0})

    shape = learn_diurnal_shape([usable, no_anchor], anchor_minute=ANCHOR)

    assert shape.day_count == 1


def test_history_without_the_anchor_anywhere_is_refused() -> None:
    with pytest.raises(InsufficientHistoryError, match="anchor minute"):
        learn_diurnal_shape(
            [day({9 * 60: 33.0, 10 * 60: 35.0})], anchor_minute=ANCHOR
        )


def test_empty_history_is_refused() -> None:
    with pytest.raises(InsufficientHistoryError, match="at least one"):
        learn_diurnal_shape([], anchor_minute=ANCHOR)


def test_residuals_come_from_held_out_days_and_exclude_the_anchor() -> None:
    shape = learn_diurnal_shape([day(ramp(30.0, 2.0))], anchor_minute=ANCHOR)
    # Held-out day warms faster than history, so predictions run low.
    held_out = [day(ramp(31.0, 3.0))]

    residuals = shape_residuals(shape, held_out)

    # The anchor is excluded because its error is zero by construction and
    # would otherwise dilute the calibration set.
    assert len(residuals) == 2 * 4
    assert all(value < 0 for value in residuals)
    assert min(residuals) == pytest.approx(-4.0)


def test_calibrated_interval_covers_its_calibration_residuals() -> None:
    shape = learn_diurnal_shape([day(ramp(30.0, 2.0))], anchor_minute=ANCHOR)
    residuals = [0.2, -0.4, 0.8, -1.1, 0.3, 0.5, -0.6, 0.9, 1.4, -0.2]

    forecast = calibrate_forecast(shape, 35.0, residuals, miscoverage=0.2)

    assert forecast.calibration_sample_count == 10
    assert forecast.radius_c > 0
    assert empirical_coverage(residuals, forecast.radius_c) >= 0.8


def test_conservative_profile_is_the_upper_bound() -> None:
    shape = learn_diurnal_shape([day(ramp(30.0, 2.0))], anchor_minute=ANCHOR)
    forecast = calibrate_forecast(shape, 35.0, [0.5, -0.5, 1.0], miscoverage=0.25)

    conservative = forecast.to_profile("A", conservative=True)
    expected = forecast.to_profile("A", conservative=False)

    hot, _ = conservative.condition_at(10 * 60)
    mid, _ = expected.condition_at(10 * 60)
    assert hot == pytest.approx(mid + forecast.radius_c)
    # Planning against the upper bound is what makes the schedule cautious.
    assert hot > mid


def test_too_few_residuals_for_the_requested_confidence_is_refused() -> None:
    shape = learn_diurnal_shape([day(ramp(30.0, 2.0))], anchor_minute=ANCHOR)

    # 99% coverage needs ceil((n+1)*0.99) <= n, impossible with 3 samples.
    with pytest.raises(Exception, match="cannot support"):
        calibrate_forecast(shape, 35.0, [0.1, 0.2, 0.3], miscoverage=0.01)


def test_coverage_helper_validates_its_inputs() -> None:
    with pytest.raises(ValueError, match="at least one residual"):
        empirical_coverage([], 1.0)
    with pytest.raises(ValueError, match="cannot be negative"):
        empirical_coverage([0.5], -1.0)
