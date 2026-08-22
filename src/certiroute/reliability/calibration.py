"""Leakage-safe split conformal calibration for black-box forecasts."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime

from certiroute.reliability.models import (
    FiniteSampleQuantile,
    HorizonBand,
    VendorRelativeConformalCalibration,
    VendorRelativeConformalGroup,
    VendorRelativeEvaluationMetrics,
    VendorRelativeForecastPair,
    VendorRelativePredictionInterval,
    VendorRelativeTemporalSplit,
    _require_utc_datetime,
)


class InsufficientCalibrationSamplesError(ValueError):
    """Raised when a conformal calibration group is too small."""


def split_vendor_relative_pairs_by_target_time(
    pairs: Iterable[VendorRelativeForecastPair],
    *,
    evaluation_start_target_utc: datetime,
    minimum_calibration_samples: int,
) -> VendorRelativeTemporalSplit:
    """Create a chronological holdout without using unavailable residuals.

    Evaluation membership is determined only by target valid time. Calibration
    targets must precede the cutoff, and their later-vendor values must have
    been recorded no later than the earliest held-out forecast issuance. This
    second condition prevents retrospectively collected values from leaking
    into a deployment-style backtest.
    """

    if minimum_calibration_samples < 1:
        raise ValueError("minimum_calibration_samples must be at least 1")
    cutoff = _require_utc_datetime(
        evaluation_start_target_utc,
        field_name="evaluation_start_target_utc",
    )
    ordered = tuple(
        sorted(
            pairs,
            key=lambda item: (
                item.target_valid_at_utc,
                item.forecast_issued_at_utc,
                item.pair_id,
            ),
        )
    )
    _require_unique_pair_ids(ordered)
    evaluation = tuple(item for item in ordered if item.target_valid_at_utc >= cutoff)
    if not evaluation:
        raise ValueError("target-time split produced no evaluation pairs")

    knowledge_cutoff = min(item.forecast_issued_at_utc for item in evaluation)
    historical = tuple(item for item in ordered if item.target_valid_at_utc < cutoff)
    calibration = tuple(
        item
        for item in historical
        if item.vendor_relative_recorded_at_utc <= knowledge_cutoff
    )
    unavailable_count = len(historical) - len(calibration)
    _guard_minimum_samples(
        len(calibration),
        minimum_calibration_samples,
        group="pooled temporal calibration split",
    )
    return VendorRelativeTemporalSplit(
        evaluation_start_target_utc=cutoff,
        knowledge_cutoff_utc=knowledge_cutoff,
        calibration_pairs=calibration,
        evaluation_pairs=evaluation,
        excluded_unavailable_calibration_count=unavailable_count,
    )


def finite_sample_absolute_residual_quantile(
    absolute_residuals_c: Iterable[float],
    *,
    miscoverage: float,
) -> FiniteSampleQuantile:
    """Return the split-conformal ``ceil((n+1)(1-alpha))`` order statistic."""

    if not 0 < miscoverage < 1:
        raise ValueError("miscoverage must be strictly between 0 and 1")
    residuals = tuple(float(value) for value in absolute_residuals_c)
    if not residuals:
        raise InsufficientCalibrationSamplesError(
            "finite-sample quantile requires at least one residual"
        )
    if any(not math.isfinite(value) or value < 0 for value in residuals):
        raise ValueError("absolute residuals must be finite and non-negative")

    sample_count = len(residuals)
    rank = math.ceil((sample_count + 1) * (1 - miscoverage))
    if rank > sample_count:
        raise InsufficientCalibrationSamplesError(
            f"{sample_count} samples cannot support miscoverage={miscoverage:g} "
            "with a finite split-conformal radius"
        )
    return FiniteSampleQuantile(
        sample_count=sample_count,
        order_statistic_rank=rank,
        absolute_residual_quantile_c=sorted(residuals)[rank - 1],
    )


def calibrate_pooled_vendor_relative_intervals(
    calibration_pairs: Sequence[VendorRelativeForecastPair],
    *,
    miscoverage: float = 0.1,
    minimum_calibration_samples: int = 30,
) -> VendorRelativeConformalCalibration:
    """Fit one symmetric absolute-residual radius across all horizons."""

    pairs = tuple(calibration_pairs)
    _validate_calibration_input(
        pairs,
        minimum_calibration_samples=minimum_calibration_samples,
        group="pooled",
    )
    quantile = finite_sample_absolute_residual_quantile(
        (abs(item.vendor_relative_residual_c) for item in pairs),
        miscoverage=miscoverage,
    )
    return _calibration_record(
        method="pooled_absolute_residual",
        pairs=pairs,
        miscoverage=miscoverage,
        minimum_calibration_samples=minimum_calibration_samples,
        groups=(
            VendorRelativeConformalGroup(
                label="pooled",
                calibration_sample_count=quantile.sample_count,
                finite_sample_rank=quantile.order_statistic_rank,
                symmetric_radius_c=quantile.absolute_residual_quantile_c,
            ),
        ),
    )


def calibrate_horizon_conditioned_vendor_relative_intervals(
    calibration_pairs: Sequence[VendorRelativeForecastPair],
    *,
    horizon_bands: Sequence[HorizonBand],
    miscoverage: float = 0.1,
    minimum_calibration_samples: int = 30,
) -> VendorRelativeConformalCalibration:
    """Fit a separate symmetric radius in each predeclared lead-time band."""

    pairs = tuple(calibration_pairs)
    bands = tuple(horizon_bands)
    _validate_calibration_input(
        pairs,
        minimum_calibration_samples=minimum_calibration_samples,
        group="all horizon bands",
    )
    _validate_horizon_bands(bands)

    grouped: dict[str, list[VendorRelativeForecastPair]] = {
        band.label: [] for band in bands
    }
    for pair in pairs:
        band = _matching_band(pair.assumed_lead_hours, bands)
        grouped[band.label].append(pair)

    groups = []
    for band in bands:
        band_pairs = grouped[band.label]
        _guard_minimum_samples(
            len(band_pairs),
            minimum_calibration_samples,
            group=f"horizon band {band.label!r}",
        )
        quantile = finite_sample_absolute_residual_quantile(
            (abs(item.vendor_relative_residual_c) for item in band_pairs),
            miscoverage=miscoverage,
        )
        groups.append(
            VendorRelativeConformalGroup(
                label=band.label,
                horizon_band=band,
                calibration_sample_count=quantile.sample_count,
                finite_sample_rank=quantile.order_statistic_rank,
                symmetric_radius_c=quantile.absolute_residual_quantile_c,
            )
        )

    return _calibration_record(
        method="horizon_conditioned_absolute_residual",
        pairs=pairs,
        miscoverage=miscoverage,
        minimum_calibration_samples=minimum_calibration_samples,
        groups=tuple(groups),
    )


def build_vendor_relative_prediction_intervals(
    calibration: VendorRelativeConformalCalibration,
    evaluation_pairs: Iterable[VendorRelativeForecastPair],
) -> tuple[VendorRelativePredictionInterval, ...]:
    """Apply a fitted calibration to later-vendor evaluation pairs."""

    pairs = tuple(evaluation_pairs)
    _require_unique_pair_ids(pairs)
    intervals = []
    for pair in pairs:
        group = _calibration_group_for_pair(calibration, pair)
        radius = group.symmetric_radius_c
        intervals.append(
            VendorRelativePredictionInterval(
                pair_id=pair.pair_id,
                geography=pair.geography,
                target_valid_at_utc=pair.target_valid_at_utc,
                assumed_lead_hours=pair.assumed_lead_hours,
                calibration_group=group.label,
                forecast_temperature_c=pair.forecast_temperature_c,
                vendor_relative_realization_temperature_c=(
                    pair.vendor_relative_realization_temperature_c
                ),
                lower_temperature_c=pair.forecast_temperature_c - radius,
                upper_temperature_c=pair.forecast_temperature_c + radius,
            )
        )
    return tuple(intervals)


def evaluate_vendor_relative_intervals(
    intervals: Sequence[VendorRelativePredictionInterval],
    *,
    screening_threshold_c: float,
) -> VendorRelativeEvaluationMetrics:
    """Measure held-out coverage, sharpness, residuals, bias, and hot misses."""

    values = tuple(intervals)
    if not values:
        raise ValueError("evaluation requires at least one prediction interval")
    threshold = float(screening_threshold_c)
    if not math.isfinite(threshold):
        raise ValueError("screening_threshold_c must be finite")

    residuals = tuple(item.vendor_relative_residual_c for item in values)
    exceedances = tuple(
        item
        for item in values
        if item.vendor_relative_realization_temperature_c >= threshold
    )
    point_misses = sum(item.forecast_temperature_c < threshold for item in exceedances)
    upper_misses = sum(item.upper_temperature_c < threshold for item in exceedances)
    denominator = len(exceedances)
    return VendorRelativeEvaluationMetrics(
        sample_count=len(values),
        empirical_interval_coverage=(
            sum(item.contains_vendor_relative_realization for item in values)
            / len(values)
        ),
        mean_interval_width_c=(
            sum(item.upper_temperature_c - item.lower_temperature_c for item in values)
            / len(values)
        ),
        mean_absolute_vendor_relative_residual_c=(
            sum(abs(value) for value in residuals) / len(residuals)
        ),
        mean_vendor_relative_residual_c=sum(residuals) / len(residuals),
        screening_threshold_c=threshold,
        vendor_relative_threshold_exceedance_count=denominator,
        point_forecast_threshold_miss_count=point_misses,
        point_forecast_threshold_miss_rate=(
            point_misses / denominator if denominator else None
        ),
        upper_interval_threshold_miss_count=upper_misses,
        upper_interval_threshold_miss_rate=(
            upper_misses / denominator if denominator else None
        ),
    )


def _calibration_record(
    *,
    method: str,
    pairs: tuple[VendorRelativeForecastPair, ...],
    miscoverage: float,
    minimum_calibration_samples: int,
    groups: tuple[VendorRelativeConformalGroup, ...],
) -> VendorRelativeConformalCalibration:
    targets = tuple(item.target_valid_at_utc for item in pairs)
    return VendorRelativeConformalCalibration(
        method=method,
        miscoverage=miscoverage,
        minimum_calibration_samples=minimum_calibration_samples,
        groups=groups,
        calibration_pair_ids=tuple(item.pair_id for item in pairs),
        calibration_target_start_utc=min(targets),
        calibration_target_end_utc=max(targets),
    )


def _validate_calibration_input(
    pairs: tuple[VendorRelativeForecastPair, ...],
    *,
    minimum_calibration_samples: int,
    group: str,
) -> None:
    if minimum_calibration_samples < 1:
        raise ValueError("minimum_calibration_samples must be at least 1")
    _require_unique_pair_ids(pairs)
    _guard_minimum_samples(len(pairs), minimum_calibration_samples, group=group)


def _guard_minimum_samples(actual: int, required: int, *, group: str) -> None:
    if actual < required:
        raise InsufficientCalibrationSamplesError(
            f"{group} has {actual} calibration samples; at least {required} required"
        )


def _require_unique_pair_ids(pairs: Sequence[VendorRelativeForecastPair]) -> None:
    identifiers = [item.pair_id for item in pairs]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError(
            "vendor-relative forecast pairs contain duplicate pair_id values"
        )


def _validate_horizon_bands(bands: tuple[HorizonBand, ...]) -> None:
    if not bands:
        raise ValueError("horizon-conditioned calibration needs at least one band")
    if len({band.label for band in bands}) != len(bands):
        raise ValueError("horizon band labels must be unique")
    ordered = sorted(bands, key=lambda item: item.minimum_lead_hours)
    for left, right in zip(ordered, ordered[1:], strict=False):
        if left.maximum_lead_hours is None:
            raise ValueError("an unbounded horizon band must be last")
        if left.maximum_lead_hours > right.minimum_lead_hours:
            raise ValueError("horizon bands cannot overlap")


def _matching_band(
    assumed_lead_hours: float,
    bands: Sequence[HorizonBand],
) -> HorizonBand:
    matches = tuple(band for band in bands if band.contains(assumed_lead_hours))
    if len(matches) != 1:
        raise ValueError(
            f"assumed lead {assumed_lead_hours:g}h must match exactly one horizon band"
        )
    return matches[0]


def _calibration_group_for_pair(
    calibration: VendorRelativeConformalCalibration,
    pair: VendorRelativeForecastPair,
) -> VendorRelativeConformalGroup:
    if calibration.method == "pooled_absolute_residual":
        return calibration.groups[0]
    bands = tuple(
        group.horizon_band
        for group in calibration.groups
        if group.horizon_band is not None
    )
    band = _matching_band(pair.assumed_lead_hours, bands)
    return next(group for group in calibration.groups if group.label == band.label)
