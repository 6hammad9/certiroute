"""Exact threshold-crossing times for calibrated future temperature curves."""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)

from certiroute.reliability.models import (
    VendorRelativeConformalCalibration,
    VendorRelativeConformalGroup,
    _format_utc_datetime,
    _require_utc_datetime,
)


class ForecastThresholdStatus(StrEnum):
    """The strongest threshold-crossing statement supported by the curves."""

    EXPECTED = "expected"
    POSSIBLE_ONLY = "possible_only"
    NO_CROSSING = "no_crossing"


class UnrealizedTemperatureForecastPoint(BaseModel):
    """One future vendor point forecast with no realization attached."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    forecast_issued_at_utc: datetime
    target_valid_at_utc: datetime
    forecast_temperature_c: float

    @field_validator("forecast_issued_at_utc", "target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer("forecast_issued_at_utc", "target_valid_at_utc", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @model_validator(mode="after")
    def validate_future_target(self) -> UnrealizedTemperatureForecastPoint:
        if self.target_valid_at_utc <= self.forecast_issued_at_utc:
            raise ValueError("forecast target must be strictly after issuance")
        return self

    @property
    def assumed_lead_hours(self) -> float:
        return (
            self.target_valid_at_utc - self.forecast_issued_at_utc
        ).total_seconds() / 3600


class CalibratedFutureTemperaturePoint(BaseModel):
    """One future point forecast with calibrated lower and upper bounds."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    forecast_issued_at_utc: datetime
    target_valid_at_utc: datetime
    forecast_temperature_c: float
    lower_temperature_c: float
    upper_temperature_c: float

    @field_validator("forecast_issued_at_utc", "target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer("forecast_issued_at_utc", "target_valid_at_utc", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @model_validator(mode="after")
    def validate_future_interval(self) -> CalibratedFutureTemperaturePoint:
        if self.target_valid_at_utc <= self.forecast_issued_at_utc:
            raise ValueError("forecast target must be strictly after issuance")
        if self.lower_temperature_c > self.upper_temperature_c:
            raise ValueError("calibrated lower bound cannot exceed upper bound")
        if not (
            self.lower_temperature_c
            <= self.forecast_temperature_c
            <= self.upper_temperature_c
        ):
            raise ValueError("calibrated interval must contain the point forecast")
        return self


def apply_vendor_relative_calibration_to_future_points(
    calibration: VendorRelativeConformalCalibration,
    points: Sequence[UnrealizedTemperatureForecastPoint],
) -> tuple[CalibratedFutureTemperaturePoint, ...]:
    """Apply same-vendor residual calibration without requiring a realization.

    The resulting bounds describe consistency with later values from the same
    vendor. They are not validated against independent sensor ground truth.
    """

    values = tuple(points)
    if not values:
        raise ValueError("calibration requires at least one unrealized future point")
    issuance = values[0].forecast_issued_at_utc
    if any(item.forecast_issued_at_utc != issuance for item in values):
        raise ValueError("all future points must belong to one forecast issuance")
    targets = tuple(item.target_valid_at_utc for item in values)
    if len(set(targets)) != len(targets):
        raise ValueError("future forecast points cannot have duplicate target times")
    if targets != tuple(sorted(targets)):
        raise ValueError("future forecast points must be ordered by target time")
    if calibration.calibration_target_end_utc >= issuance:
        raise ValueError(
            "vendor-relative calibration targets must precede forecast issuance"
        )

    calibrated = []
    for point in values:
        group = _vendor_relative_group_for_lead(calibration, point.assumed_lead_hours)
        radius = group.symmetric_radius_c
        calibrated.append(
            CalibratedFutureTemperaturePoint(
                forecast_issued_at_utc=point.forecast_issued_at_utc,
                target_valid_at_utc=point.target_valid_at_utc,
                forecast_temperature_c=point.forecast_temperature_c,
                lower_temperature_c=point.forecast_temperature_c - radius,
                upper_temperature_c=point.forecast_temperature_c + radius,
            )
        )
    return tuple(calibrated)


class ThresholdCrossingPrediction(BaseModel):
    """First upward crossing on each piecewise-linear forecast curve.

    For an expected crossing, warning lead is measured to the central point
    forecast crossing. For a possible-only crossing, it is measured to the
    upper-bound crossing that triggered that classification.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    status: ForecastThresholdStatus
    threshold_c: float
    forecast_issued_at_utc: datetime
    forecast_window_start_utc: datetime
    forecast_window_end_utc: datetime
    point_crossing_at_utc: datetime | None = None
    lower_bound_crossing_at_utc: datetime | None = None
    upper_bound_crossing_at_utc: datetime | None = None
    warning_crossing_at_utc: datetime | None = None
    warning_lead_minutes: float | None = Field(default=None, ge=0)

    @field_validator(
        "forecast_issued_at_utc",
        "forecast_window_start_utc",
        "forecast_window_end_utc",
        "point_crossing_at_utc",
        "lower_bound_crossing_at_utc",
        "upper_bound_crossing_at_utc",
        "warning_crossing_at_utc",
        mode="before",
    )
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime | None:
        if value is None:
            return None
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "forecast_issued_at_utc",
        "forecast_window_start_utc",
        "forecast_window_end_utc",
        "point_crossing_at_utc",
        "lower_bound_crossing_at_utc",
        "upper_bound_crossing_at_utc",
        "warning_crossing_at_utc",
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        return _format_utc_datetime(value) if value is not None else None

    @model_validator(mode="after")
    def validate_crossing_consistency(self) -> ThresholdCrossingPrediction:
        if self.forecast_window_start_utc > self.forecast_window_end_utc:
            raise ValueError("forecast window start cannot follow its end")
        crossings = (
            self.point_crossing_at_utc,
            self.lower_bound_crossing_at_utc,
            self.upper_bound_crossing_at_utc,
        )
        if any(
            crossing is not None
            and not (
                self.forecast_window_start_utc
                <= crossing
                <= self.forecast_window_end_utc
            )
            for crossing in crossings
        ):
            raise ValueError("crossing times must lie inside the forecast window")

        expected_warning = None
        if self.status is ForecastThresholdStatus.EXPECTED:
            if self.point_crossing_at_utc is None:
                raise ValueError("expected status requires a point-forecast crossing")
            expected_warning = self.point_crossing_at_utc
        elif self.status is ForecastThresholdStatus.POSSIBLE_ONLY:
            if self.point_crossing_at_utc is not None:
                raise ValueError("possible-only status cannot have a point crossing")
            if self.upper_bound_crossing_at_utc is None:
                raise ValueError(
                    "possible-only status requires an upper-bound crossing"
                )
            if self.lower_bound_crossing_at_utc is not None:
                raise ValueError(
                    "possible-only status cannot have a lower-bound crossing"
                )
            expected_warning = self.upper_bound_crossing_at_utc
        elif any(crossing is not None for crossing in crossings):
            raise ValueError("no-crossing status cannot contain crossing times")

        if self.warning_crossing_at_utc != expected_warning:
            raise ValueError("warning crossing is inconsistent with crossing status")
        if self.point_crossing_at_utc is not None and (
            self.upper_bound_crossing_at_utc is None
            or self.upper_bound_crossing_at_utc > self.point_crossing_at_utc
        ):
            raise ValueError("upper-bound crossing cannot follow the point crossing")
        if self.lower_bound_crossing_at_utc is not None and (
            self.point_crossing_at_utc is None
            or self.lower_bound_crossing_at_utc < self.point_crossing_at_utc
        ):
            raise ValueError("lower-bound crossing cannot precede the point crossing")
        expected_lead = (
            (expected_warning - self.forecast_issued_at_utc).total_seconds() / 60
            if expected_warning is not None
            else None
        )
        if self.warning_lead_minutes is None and expected_lead is not None:
            raise ValueError("warning lead is required when a warning crossing exists")
        if self.warning_lead_minutes is not None and (
            expected_lead is None
            or not math.isclose(
                self.warning_lead_minutes,
                expected_lead,
                rel_tol=1e-12,
                abs_tol=1e-9,
            )
        ):
            raise ValueError("warning lead must equal crossing minus issuance")
        return self


def predict_future_threshold_crossing(
    points: Sequence[CalibratedFutureTemperaturePoint],
    *,
    threshold_c: float,
) -> ThresholdCrossingPrediction:
    """Find the first upward threshold crossing on point/lower/upper curves.

    Curves are linearly interpolated between the supplied target times. If the
    first value is already at or above the threshold, its target time is the
    earliest crossing identifiable from this forecast window.
    """

    values = tuple(points)
    if not values:
        raise ValueError("threshold prediction requires at least one future point")
    threshold = float(threshold_c)
    if not math.isfinite(threshold):
        raise ValueError("threshold_c must be finite")

    issuance = values[0].forecast_issued_at_utc
    if any(item.forecast_issued_at_utc != issuance for item in values):
        raise ValueError("all future points must belong to one forecast issuance")
    targets = tuple(item.target_valid_at_utc for item in values)
    if len(set(targets)) != len(targets):
        raise ValueError("future forecast points cannot have duplicate target times")
    if targets != tuple(sorted(targets)):
        raise ValueError("future forecast points must be ordered by target time")

    point_crossing = _first_linear_crossing(
        values, threshold=threshold, temperature_field="forecast_temperature_c"
    )
    lower_crossing = _first_linear_crossing(
        values, threshold=threshold, temperature_field="lower_temperature_c"
    )
    upper_crossing = _first_linear_crossing(
        values, threshold=threshold, temperature_field="upper_temperature_c"
    )

    if point_crossing is not None:
        status = ForecastThresholdStatus.EXPECTED
        warning_crossing = point_crossing
    elif upper_crossing is not None:
        status = ForecastThresholdStatus.POSSIBLE_ONLY
        warning_crossing = upper_crossing
    else:
        status = ForecastThresholdStatus.NO_CROSSING
        warning_crossing = None
    warning_lead = (
        (warning_crossing - issuance).total_seconds() / 60
        if warning_crossing is not None
        else None
    )
    return ThresholdCrossingPrediction(
        status=status,
        threshold_c=threshold,
        forecast_issued_at_utc=issuance,
        forecast_window_start_utc=targets[0],
        forecast_window_end_utc=targets[-1],
        point_crossing_at_utc=point_crossing,
        lower_bound_crossing_at_utc=lower_crossing,
        upper_bound_crossing_at_utc=upper_crossing,
        warning_crossing_at_utc=warning_crossing,
        warning_lead_minutes=warning_lead,
    )


def _first_linear_crossing(
    points: tuple[CalibratedFutureTemperaturePoint, ...],
    *,
    threshold: float,
    temperature_field: str,
) -> datetime | None:
    first_temperature = float(getattr(points[0], temperature_field))
    if first_temperature >= threshold:
        return points[0].target_valid_at_utc

    for left, right in zip(points, points[1:], strict=False):
        left_temperature = float(getattr(left, temperature_field))
        right_temperature = float(getattr(right, temperature_field))
        if left_temperature < threshold <= right_temperature:
            fraction = (threshold - left_temperature) / (
                right_temperature - left_temperature
            )
            seconds = (
                right.target_valid_at_utc - left.target_valid_at_utc
            ).total_seconds()
            return left.target_valid_at_utc + timedelta(seconds=seconds * fraction)
    return None


def _vendor_relative_group_for_lead(
    calibration: VendorRelativeConformalCalibration,
    assumed_lead_hours: float,
) -> VendorRelativeConformalGroup:
    if calibration.method == "pooled_absolute_residual":
        if len(calibration.groups) != 1:
            raise ValueError("pooled calibration must contain exactly one group")
        return calibration.groups[0]

    matches = tuple(
        group
        for group in calibration.groups
        if group.horizon_band is not None
        and group.horizon_band.contains(assumed_lead_hours)
    )
    if len(matches) != 1:
        raise ValueError(
            f"assumed lead {assumed_lead_hours:g}h must match exactly one "
            "vendor-relative horizon group"
        )
    return matches[0]
