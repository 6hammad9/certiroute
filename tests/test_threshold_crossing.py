from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from certiroute.reliability import (
    CalibratedFutureTemperaturePoint,
    ForecastThresholdStatus,
    HorizonBand,
    UnrealizedTemperatureForecastPoint,
    VendorRelativeConformalCalibration,
    VendorRelativeConformalGroup,
    apply_vendor_relative_calibration_to_future_points,
    predict_future_threshold_crossing,
)

ISSUED = datetime(2026, 8, 22, 8, tzinfo=UTC)


def _point(
    hour: int,
    *,
    forecast: float,
    lower: float,
    upper: float,
    issued: datetime = ISSUED,
) -> CalibratedFutureTemperaturePoint:
    return CalibratedFutureTemperaturePoint(
        forecast_issued_at_utc=issued,
        target_valid_at_utc=datetime(2026, 8, 22, hour, tzinfo=UTC),
        forecast_temperature_c=forecast,
        lower_temperature_c=lower,
        upper_temperature_c=upper,
    )


def _unrealized(
    hour: int,
    *,
    forecast: float,
    issued: datetime = ISSUED,
) -> UnrealizedTemperatureForecastPoint:
    return UnrealizedTemperatureForecastPoint(
        forecast_issued_at_utc=issued,
        target_valid_at_utc=datetime(2026, 8, 22, hour, tzinfo=UTC),
        forecast_temperature_c=forecast,
    )


def _calibration(
    *groups: VendorRelativeConformalGroup,
) -> VendorRelativeConformalCalibration:
    conditioned = any(group.horizon_band is not None for group in groups)
    return VendorRelativeConformalCalibration(
        method=(
            "horizon_conditioned_absolute_residual"
            if conditioned
            else "pooled_absolute_residual"
        ),
        miscoverage=0.25,
        minimum_calibration_samples=4,
        groups=groups,
        calibration_pair_ids=("one", "two", "three", "four"),
        calibration_target_start_utc=ISSUED - timedelta(days=5),
        calibration_target_end_utc=ISSUED - timedelta(days=1),
    )


def test_applies_pooled_vendor_relative_radius_without_a_realization() -> None:
    calibration = _calibration(
        VendorRelativeConformalGroup(
            label="pooled",
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=2,
        )
    )
    future = (
        _unrealized(9, forecast=33),
        _unrealized(10, forecast=36),
    )

    calibrated = apply_vendor_relative_calibration_to_future_points(calibration, future)
    crossing = predict_future_threshold_crossing(calibrated, threshold_c=35)

    assert [
        (item.lower_temperature_c, item.upper_temperature_c) for item in calibrated
    ] == [
        (31, 35),
        (34, 38),
    ]
    assert crossing.status is ForecastThresholdStatus.EXPECTED
    assert crossing.point_crossing_at_utc == datetime(2026, 8, 22, 9, 40, tzinfo=UTC)
    assert (
        "vendor_relative_realization_temperature_c"
        not in UnrealizedTemperatureForecastPoint.model_fields
    )
    with pytest.raises(ValidationError, match="frozen"):
        future[0].forecast_temperature_c = 99


def test_applies_exactly_one_horizon_group_with_left_closed_boundary() -> None:
    calibration = _calibration(
        VendorRelativeConformalGroup(
            label="near",
            horizon_band=HorizonBand(
                label="near", minimum_lead_hours=0, maximum_lead_hours=3
            ),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=1,
        ),
        VendorRelativeConformalGroup(
            label="far",
            horizon_band=HorizonBand(label="far", minimum_lead_hours=3),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=4,
        ),
    )

    calibrated = apply_vendor_relative_calibration_to_future_points(
        calibration,
        [_unrealized(9, forecast=33), _unrealized(11, forecast=34)],
    )

    assert calibrated[0].lower_temperature_c == 32
    assert calibrated[0].upper_temperature_c == 34
    assert calibrated[1].lower_temperature_c == 30
    assert calibrated[1].upper_temperature_c == 38


def test_future_calibration_rejects_missing_or_ambiguous_horizon_group() -> None:
    gap_calibration = _calibration(
        VendorRelativeConformalGroup(
            label="near",
            horizon_band=HorizonBand(
                label="near", minimum_lead_hours=0, maximum_lead_hours=2
            ),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=1,
        ),
        VendorRelativeConformalGroup(
            label="far",
            horizon_band=HorizonBand(label="far", minimum_lead_hours=3),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=3,
        ),
    )
    future = UnrealizedTemperatureForecastPoint(
        forecast_issued_at_utc=ISSUED,
        target_valid_at_utc=ISSUED + timedelta(hours=2.5),
        forecast_temperature_c=34,
    )

    with pytest.raises(ValueError, match="must match exactly one"):
        apply_vendor_relative_calibration_to_future_points(gap_calibration, [future])

    overlapping_calibration = _calibration(
        VendorRelativeConformalGroup(
            label="first",
            horizon_band=HorizonBand(
                label="first", minimum_lead_hours=0, maximum_lead_hours=4
            ),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=1,
        ),
        VendorRelativeConformalGroup(
            label="second",
            horizon_band=HorizonBand(label="second", minimum_lead_hours=2),
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=3,
        ),
    )
    with pytest.raises(ValueError, match="must match exactly one"):
        apply_vendor_relative_calibration_to_future_points(
            overlapping_calibration, [future]
        )


def test_future_calibration_rejects_temporal_inconsistencies() -> None:
    calibration = _calibration(
        VendorRelativeConformalGroup(
            label="pooled",
            calibration_sample_count=4,
            finite_sample_rank=4,
            symmetric_radius_c=2,
        )
    )
    nine = _unrealized(9, forecast=33)
    ten = _unrealized(10, forecast=34)

    with pytest.raises(ValueError, match="duplicate target times"):
        apply_vendor_relative_calibration_to_future_points(calibration, [nine, nine])
    with pytest.raises(ValueError, match="ordered by target time"):
        apply_vendor_relative_calibration_to_future_points(calibration, [ten, nine])
    with pytest.raises(ValueError, match="one forecast issuance"):
        apply_vendor_relative_calibration_to_future_points(
            calibration,
            [
                nine,
                _unrealized(
                    10,
                    forecast=34,
                    issued=ISSUED + timedelta(minutes=5),
                ),
            ],
        )

    future_calibration = calibration.model_copy(
        update={"calibration_target_end_utc": ISSUED}
    )
    with pytest.raises(ValueError, match="must precede forecast issuance"):
        apply_vendor_relative_calibration_to_future_points(future_calibration, [nine])


def test_unrealized_point_rejects_non_future_target() -> None:
    with pytest.raises(ValidationError, match="strictly after issuance"):
        UnrealizedTemperatureForecastPoint(
            forecast_issued_at_utc=ISSUED,
            target_valid_at_utc=ISSUED,
            forecast_temperature_c=34,
        )


def test_exactly_interpolates_each_curve_and_expected_warning_lead() -> None:
    points = [
        _point(9, forecast=30, lower=28, upper=32),
        _point(10, forecast=36, lower=34, upper=38),
        _point(11, forecast=40, lower=40, upper=42),
    ]

    prediction = predict_future_threshold_crossing(points, threshold_c=34)

    assert prediction.status is ForecastThresholdStatus.EXPECTED
    assert prediction.upper_bound_crossing_at_utc == datetime(
        2026, 8, 22, 9, 20, tzinfo=UTC
    )
    assert prediction.point_crossing_at_utc == datetime(2026, 8, 22, 9, 40, tzinfo=UTC)
    assert prediction.lower_bound_crossing_at_utc == datetime(
        2026, 8, 22, 10, tzinfo=UTC
    )
    assert prediction.warning_crossing_at_utc == prediction.point_crossing_at_utc
    assert prediction.warning_lead_minutes == pytest.approx(100)


def test_possible_only_uses_upper_bound_crossing_for_warning() -> None:
    points = [
        _point(9, forecast=30, lower=28, upper=32),
        _point(10, forecast=33, lower=31, upper=36),
    ]

    prediction = predict_future_threshold_crossing(points, threshold_c=35)

    assert prediction.status is ForecastThresholdStatus.POSSIBLE_ONLY
    assert prediction.point_crossing_at_utc is None
    assert prediction.lower_bound_crossing_at_utc is None
    assert prediction.upper_bound_crossing_at_utc == datetime(
        2026, 8, 22, 9, 45, tzinfo=UTC
    )
    assert prediction.warning_crossing_at_utc == (
        prediction.upper_bound_crossing_at_utc
    )
    assert prediction.warning_lead_minutes == pytest.approx(105)


def test_no_crossing_has_no_warning_or_lead() -> None:
    prediction = predict_future_threshold_crossing(
        [
            _point(9, forecast=30, lower=28, upper=31),
            _point(10, forecast=32, lower=30, upper=34),
        ],
        threshold_c=35,
    )

    assert prediction.status is ForecastThresholdStatus.NO_CROSSING
    assert prediction.point_crossing_at_utc is None
    assert prediction.lower_bound_crossing_at_utc is None
    assert prediction.upper_bound_crossing_at_utc is None
    assert prediction.warning_crossing_at_utc is None
    assert prediction.warning_lead_minutes is None


def test_first_point_above_threshold_is_earliest_identifiable_crossing() -> None:
    prediction = predict_future_threshold_crossing(
        [
            _point(9, forecast=36, lower=34, upper=38),
            _point(10, forecast=38, lower=36, upper=40),
        ],
        threshold_c=35,
    )

    assert prediction.status is ForecastThresholdStatus.EXPECTED
    assert prediction.point_crossing_at_utc == datetime(2026, 8, 22, 9, tzinfo=UTC)
    assert prediction.upper_bound_crossing_at_utc == datetime(
        2026, 8, 22, 9, tzinfo=UTC
    )
    assert prediction.lower_bound_crossing_at_utc == datetime(
        2026, 8, 22, 9, 30, tzinfo=UTC
    )
    assert prediction.warning_lead_minutes == 60


def test_rejects_duplicate_and_unordered_target_times() -> None:
    nine = _point(9, forecast=30, lower=28, upper=32)
    ten = _point(10, forecast=33, lower=31, upper=35)

    with pytest.raises(ValueError, match="duplicate target times"):
        predict_future_threshold_crossing([nine, nine], threshold_c=35)
    with pytest.raises(ValueError, match="ordered by target time"):
        predict_future_threshold_crossing([ten, nine], threshold_c=35)


def test_rejects_mixed_forecast_issuance_vintages() -> None:
    with pytest.raises(ValueError, match="one forecast issuance"):
        predict_future_threshold_crossing(
            [
                _point(9, forecast=30, lower=28, upper=32),
                _point(
                    10,
                    forecast=33,
                    lower=31,
                    upper=35,
                    issued=ISSUED + timedelta(minutes=5),
                ),
            ],
            threshold_c=35,
        )


def test_point_model_rejects_non_future_targets_and_invalid_intervals() -> None:
    common = {
        "forecast_issued_at_utc": ISSUED,
        "target_valid_at_utc": ISSUED + timedelta(hours=1),
        "forecast_temperature_c": 34,
        "lower_temperature_c": 32,
        "upper_temperature_c": 36,
    }

    with pytest.raises(ValidationError, match="strictly after issuance"):
        CalibratedFutureTemperaturePoint(**{**common, "target_valid_at_utc": ISSUED})
    with pytest.raises(ValidationError, match="lower bound cannot exceed"):
        CalibratedFutureTemperaturePoint(
            **{
                **common,
                "lower_temperature_c": 37,
                "upper_temperature_c": 36,
            }
        )
    with pytest.raises(ValidationError, match="must contain the point forecast"):
        CalibratedFutureTemperaturePoint(
            **{
                **common,
                "lower_temperature_c": 35,
                "upper_temperature_c": 36,
            }
        )


def test_rejects_empty_input_and_non_finite_threshold() -> None:
    with pytest.raises(ValueError, match="at least one future point"):
        predict_future_threshold_crossing([], threshold_c=35)
    with pytest.raises(ValueError, match="threshold_c must be finite"):
        predict_future_threshold_crossing(
            [_point(9, forecast=30, lower=28, upper=32)],
            threshold_c=float("nan"),
        )
