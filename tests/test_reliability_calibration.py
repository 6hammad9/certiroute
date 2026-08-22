from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from certiroute.reliability import (
    HorizonBand,
    InsufficientCalibrationSamplesError,
    VendorRelativeForecastPair,
    VendorRelativePredictionInterval,
    build_vendor_relative_prediction_intervals,
    calibrate_horizon_conditioned_vendor_relative_intervals,
    calibrate_pooled_vendor_relative_intervals,
    evaluate_vendor_relative_intervals,
    finite_sample_absolute_residual_quantile,
    split_vendor_relative_pairs_by_target_time,
)


def _pair(
    pair_id: str,
    *,
    target: datetime,
    lead_hours: float = 2,
    recorded_delay_hours: float = 1,
    forecast_c: float = 35,
    residual_c: float = 0,
    geography: str = "Phoenix",
) -> VendorRelativeForecastPair:
    return VendorRelativeForecastPair(
        pair_id=pair_id,
        forecast_record_id=f"forecast-{pair_id}",
        spatial_key=f"tile-{pair_id}",
        geography=geography,
        forecast_issued_at_utc=target - timedelta(hours=lead_hours),
        target_valid_at_utc=target,
        vendor_relative_recorded_at_utc=target + timedelta(hours=recorded_delay_hours),
        assumed_lead_hours=lead_hours,
        forecast_temperature_c=forecast_c,
        vendor_relative_realization_temperature_c=forecast_c + residual_c,
    )


def test_forecast_pair_is_immutable_and_validates_timeline_and_lead() -> None:
    target = datetime(2026, 8, 10, 18, tzinfo=UTC)
    pair = _pair("one", target=target, residual_c=1.25)

    assert pair.vendor_relative_residual_c == pytest.approx(1.25)
    with pytest.raises(ValidationError, match="frozen"):
        pair.forecast_temperature_c = 99

    with pytest.raises(ValidationError, match="assumed_lead_hours"):
        VendorRelativeForecastPair(
            **{
                **pair.model_dump(),
                "assumed_lead_hours": 3,
            }
        )
    with pytest.raises(ValidationError, match="cannot be recorded before target"):
        VendorRelativeForecastPair(
            **{
                **pair.model_dump(),
                "vendor_relative_recorded_at_utc": target - timedelta(minutes=1),
            }
        )


def test_temporal_split_uses_target_time_and_excludes_unavailable_residuals() -> None:
    cutoff = datetime(2026, 8, 3, 12, tzinfo=UTC)
    available = _pair(
        "available",
        target=datetime(2026, 8, 1, 12, tzinfo=UTC),
    )
    unavailable = _pair(
        "late-recording",
        target=datetime(2026, 8, 2, 12, tzinfo=UTC),
        recorded_delay_hours=30,
    )
    at_cutoff = _pair("held-out", target=cutoff)
    later = _pair("later", target=cutoff + timedelta(days=1))

    split = split_vendor_relative_pairs_by_target_time(
        [later, unavailable, at_cutoff, available],
        evaluation_start_target_utc=cutoff,
        minimum_calibration_samples=1,
    )

    assert [item.pair_id for item in split.calibration_pairs] == ["available"]
    assert [item.pair_id for item in split.evaluation_pairs] == [
        "held-out",
        "later",
    ]
    assert split.knowledge_cutoff_utc == at_cutoff.forecast_issued_at_utc
    assert split.excluded_unavailable_calibration_count == 1
    assert max(item.target_valid_at_utc for item in split.calibration_pairs) < min(
        item.target_valid_at_utc for item in split.evaluation_pairs
    )


def test_temporal_split_has_explicit_minimum_sample_guard() -> None:
    cutoff = datetime(2026, 8, 3, 12, tzinfo=UTC)
    pairs = [
        _pair("calibration", target=cutoff - timedelta(days=1)),
        _pair("evaluation", target=cutoff),
    ]

    with pytest.raises(
        InsufficientCalibrationSamplesError,
        match="has 1 calibration samples; at least 2 required",
    ):
        split_vendor_relative_pairs_by_target_time(
            pairs,
            evaluation_start_target_utc=cutoff,
            minimum_calibration_samples=2,
        )


def test_finite_sample_quantile_uses_conformal_order_statistic() -> None:
    quantile = finite_sample_absolute_residual_quantile(range(9), miscoverage=0.2)

    assert quantile.sample_count == 9
    assert quantile.order_statistic_rank == 8
    assert quantile.absolute_residual_quantile_c == 7

    with pytest.raises(
        InsufficientCalibrationSamplesError,
        match="cannot support miscoverage=0.1",
    ):
        finite_sample_absolute_residual_quantile([1, 2, 3], miscoverage=0.1)


def test_pooled_absolute_residual_calibration_builds_symmetric_intervals() -> None:
    target = datetime(2026, 8, 1, 12, tzinfo=UTC)
    calibration_pairs = tuple(
        _pair(
            f"cal-{index}",
            target=target + timedelta(days=index),
            residual_c=residual,
        )
        for index, residual in enumerate((-1, 0, 1, 2, -3))
    )
    calibration = calibrate_pooled_vendor_relative_intervals(
        calibration_pairs,
        miscoverage=0.2,
        minimum_calibration_samples=5,
    )
    evaluation = _pair(
        "evaluation",
        target=target + timedelta(days=10),
        forecast_c=35,
        residual_c=3,
    )

    (interval,) = build_vendor_relative_prediction_intervals(calibration, [evaluation])

    assert calibration.method == "pooled_absolute_residual"
    assert calibration.nominal_coverage == pytest.approx(0.8)
    assert calibration.groups[0].finite_sample_rank == 5
    assert calibration.groups[0].symmetric_radius_c == 3
    assert interval.lower_temperature_c == 32
    assert interval.upper_temperature_c == 38
    assert interval.contains_vendor_relative_realization


def test_horizon_conditioned_calibration_uses_predeclared_band_at_boundary() -> None:
    target = datetime(2026, 8, 1, 12, tzinfo=UTC)
    bands = (
        HorizonBand(
            label="near",
            minimum_lead_hours=0,
            maximum_lead_hours=3,
        ),
        HorizonBand(label="far", minimum_lead_hours=3),
    )
    calibration_pairs = tuple(
        [
            _pair(
                f"near-{index}",
                target=target + timedelta(days=index),
                lead_hours=1,
                residual_c=residual,
            )
            for index, residual in enumerate((0.5, 1, -1.5, 2))
        ]
        + [
            _pair(
                f"far-{index}",
                target=target + timedelta(days=index),
                lead_hours=6,
                residual_c=residual,
            )
            for index, residual in enumerate((1, -2, 3, -4))
        ]
    )
    calibration = calibrate_horizon_conditioned_vendor_relative_intervals(
        calibration_pairs,
        horizon_bands=bands,
        miscoverage=0.25,
        minimum_calibration_samples=4,
    )
    boundary_pair = _pair(
        "boundary",
        target=target + timedelta(days=20),
        lead_hours=3,
    )

    (interval,) = build_vendor_relative_prediction_intervals(
        calibration, [boundary_pair]
    )

    assert [
        (group.label, group.symmetric_radius_c) for group in calibration.groups
    ] == [
        ("near", 2),
        ("far", 4),
    ]
    assert interval.calibration_group == "far"
    assert interval.lower_temperature_c == 31
    assert interval.upper_temperature_c == 39


def test_horizon_conditioned_calibration_rejects_underfilled_group() -> None:
    target = datetime(2026, 8, 1, 12, tzinfo=UTC)
    pairs = tuple(
        _pair(
            f"near-{index}",
            target=target + timedelta(days=index),
            lead_hours=1,
        )
        for index in range(4)
    )
    bands = (
        HorizonBand(label="near", minimum_lead_hours=0, maximum_lead_hours=3),
        HorizonBand(label="far", minimum_lead_hours=3),
    )

    with pytest.raises(
        InsufficientCalibrationSamplesError,
        match="horizon band 'far' has 0 calibration samples; at least 4 required",
    ):
        calibrate_horizon_conditioned_vendor_relative_intervals(
            pairs,
            horizon_bands=bands,
            miscoverage=0.25,
            minimum_calibration_samples=4,
        )


def test_held_out_metrics_report_coverage_width_bias_and_threshold_misses() -> None:
    target = datetime(2026, 8, 20, 12, tzinfo=UTC)
    intervals = (
        _interval("one", target, forecast=34, realization=36, lower=33.5, upper=34.5),
        _interval("two", target, forecast=36, realization=38, lower=34, upper=38),
        _interval("three", target, forecast=34, realization=34, lower=33, upper=35),
    )

    metrics = evaluate_vendor_relative_intervals(intervals, screening_threshold_c=35)

    assert metrics.sample_count == 3
    assert metrics.empirical_interval_coverage == pytest.approx(2 / 3)
    assert metrics.mean_interval_width_c == pytest.approx(7 / 3)
    assert metrics.mean_absolute_vendor_relative_residual_c == pytest.approx(4 / 3)
    assert metrics.mean_vendor_relative_residual_c == pytest.approx(4 / 3)
    assert metrics.vendor_relative_threshold_exceedance_count == 2
    assert metrics.point_forecast_threshold_miss_count == 1
    assert metrics.point_forecast_threshold_miss_rate == pytest.approx(0.5)
    assert metrics.upper_interval_threshold_miss_count == 1
    assert metrics.upper_interval_threshold_miss_rate == pytest.approx(0.5)


def test_threshold_miss_rates_are_undefined_without_vendor_exceedances() -> None:
    target = datetime(2026, 8, 20, 12, tzinfo=UTC)
    metrics = evaluate_vendor_relative_intervals(
        [_interval("one", target, forecast=30, realization=31, lower=29, upper=31)],
        screening_threshold_c=35,
    )

    assert metrics.vendor_relative_threshold_exceedance_count == 0
    assert metrics.point_forecast_threshold_miss_rate is None
    assert metrics.upper_interval_threshold_miss_rate is None


def _interval(
    pair_id: str,
    target: datetime,
    *,
    forecast: float,
    realization: float,
    lower: float,
    upper: float,
) -> VendorRelativePredictionInterval:
    return VendorRelativePredictionInterval(
        pair_id=pair_id,
        geography="Phoenix",
        target_valid_at_utc=target,
        assumed_lead_hours=2,
        calibration_group="pooled",
        forecast_temperature_c=forecast,
        vendor_relative_realization_temperature_c=realization,
        lower_temperature_c=lower,
        upper_temperature_c=upper,
    )
