"""Immutable records for black-box, vendor-relative forecast reliability."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)


def _require_utc_datetime(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, str):
        candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(candidate)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise TypeError(f"{field_name} must be a datetime or ISO-8601 string")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _format_utc_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class VendorRelativeForecastPair(BaseModel):
    """One point forecast joined to a later value from the same vendor.

    The later value measures FortyGuard forecast consistency. It is deliberately
    not called ground truth or an observation from an independent sensor.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    pair_id: str = Field(min_length=1, max_length=500)
    forecast_record_id: str = Field(min_length=1, max_length=500)
    spatial_key: str = Field(min_length=1, max_length=500)
    geography: str = Field(min_length=1, max_length=200)
    forecast_issued_at_utc: datetime
    target_valid_at_utc: datetime
    vendor_relative_recorded_at_utc: datetime
    assumed_lead_hours: float = Field(ge=0)
    forecast_temperature_c: float
    vendor_relative_realization_temperature_c: float

    @field_validator(
        "forecast_issued_at_utc",
        "target_valid_at_utc",
        "vendor_relative_recorded_at_utc",
        mode="before",
    )
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "forecast_issued_at_utc",
        "target_valid_at_utc",
        "vendor_relative_recorded_at_utc",
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @field_validator("pair_id", "forecast_record_id", "spatial_key", "geography")
    @classmethod
    def normalize_identifier(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier fields cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_timeline(self) -> VendorRelativeForecastPair:
        if self.forecast_issued_at_utc > self.target_valid_at_utc:
            raise ValueError("forecast issuance cannot follow its target time")
        if self.vendor_relative_recorded_at_utc < self.target_valid_at_utc:
            raise ValueError("vendor-relative value cannot be recorded before target")
        expected_lead = (
            self.target_valid_at_utc - self.forecast_issued_at_utc
        ).total_seconds() / 3600
        if not math.isclose(
            self.assumed_lead_hours, expected_lead, rel_tol=1e-12, abs_tol=1e-9
        ):
            raise ValueError("assumed_lead_hours does not match the timestamps")
        return self

    @property
    def vendor_relative_residual_c(self) -> float:
        """Return later-vendor value minus the forecast."""

        return (
            self.vendor_relative_realization_temperature_c - self.forecast_temperature_c
        )


class HorizonBand(BaseModel):
    """A left-closed, right-open assumed lead-time band."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    label: str = Field(min_length=1, max_length=100)
    minimum_lead_hours: float = Field(ge=0)
    maximum_lead_hours: float | None = Field(default=None, gt=0)

    @field_validator("label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("label cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_bounds(self) -> HorizonBand:
        if (
            self.maximum_lead_hours is not None
            and self.maximum_lead_hours <= self.minimum_lead_hours
        ):
            raise ValueError("maximum_lead_hours must exceed minimum_lead_hours")
        return self

    def contains(self, assumed_lead_hours: float) -> bool:
        return assumed_lead_hours >= self.minimum_lead_hours and (
            self.maximum_lead_hours is None
            or assumed_lead_hours < self.maximum_lead_hours
        )


class VendorRelativeTemporalSplit(BaseModel):
    """Chronological calibration/evaluation split with an availability cutoff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evaluation_start_target_utc: datetime
    knowledge_cutoff_utc: datetime
    calibration_pairs: tuple[VendorRelativeForecastPair, ...] = Field(min_length=1)
    evaluation_pairs: tuple[VendorRelativeForecastPair, ...] = Field(min_length=1)
    excluded_unavailable_calibration_count: int = Field(ge=0)

    @field_validator(
        "evaluation_start_target_utc", "knowledge_cutoff_utc", mode="before"
    )
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "evaluation_start_target_utc", "knowledge_cutoff_utc", when_used="json"
    )
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @model_validator(mode="after")
    def validate_split(self) -> VendorRelativeTemporalSplit:
        if any(
            item.target_valid_at_utc >= self.evaluation_start_target_utc
            for item in self.calibration_pairs
        ):
            raise ValueError("calibration targets must precede the evaluation cutoff")
        if any(
            item.target_valid_at_utc < self.evaluation_start_target_utc
            for item in self.evaluation_pairs
        ):
            raise ValueError("evaluation targets cannot precede the evaluation cutoff")
        if any(
            item.vendor_relative_recorded_at_utc > self.knowledge_cutoff_utc
            for item in self.calibration_pairs
        ):
            raise ValueError("calibration values must be known by the knowledge cutoff")
        expected_knowledge_cutoff = min(
            item.forecast_issued_at_utc for item in self.evaluation_pairs
        )
        if self.knowledge_cutoff_utc != expected_knowledge_cutoff:
            raise ValueError(
                "knowledge cutoff must equal the earliest evaluation issuance"
            )
        identifiers = [
            item.pair_id for item in (*self.calibration_pairs, *self.evaluation_pairs)
        ]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("calibration and evaluation pair IDs must be disjoint")
        return self


class FiniteSampleQuantile(BaseModel):
    """The order statistic used by split conformal calibration."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    sample_count: int = Field(ge=1)
    order_statistic_rank: int = Field(ge=1)
    absolute_residual_quantile_c: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_rank(self) -> FiniteSampleQuantile:
        if self.order_statistic_rank > self.sample_count:
            raise ValueError("order-statistic rank cannot exceed sample count")
        return self


class VendorRelativeConformalGroup(BaseModel):
    """One pooled or horizon-conditioned calibration group."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    label: str = Field(min_length=1)
    horizon_band: HorizonBand | None = None
    calibration_sample_count: int = Field(ge=1)
    finite_sample_rank: int = Field(ge=1)
    symmetric_radius_c: float = Field(ge=0)

    @model_validator(mode="after")
    def validate_rank(self) -> VendorRelativeConformalGroup:
        if self.finite_sample_rank > self.calibration_sample_count:
            raise ValueError("finite-sample rank cannot exceed calibration count")
        return self


class VendorRelativeConformalCalibration(BaseModel):
    """A fitted symmetric split-conformal absolute-residual calibration."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    method: Literal["pooled_absolute_residual", "horizon_conditioned_absolute_residual"]
    miscoverage: float = Field(gt=0, lt=1)
    minimum_calibration_samples: int = Field(ge=1)
    groups: tuple[VendorRelativeConformalGroup, ...] = Field(min_length=1)
    calibration_pair_ids: tuple[str, ...] = Field(min_length=1)
    calibration_target_start_utc: datetime
    calibration_target_end_utc: datetime

    @field_validator(
        "calibration_target_start_utc", "calibration_target_end_utc", mode="before"
    )
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "calibration_target_start_utc",
        "calibration_target_end_utc",
        when_used="json",
    )
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @model_validator(mode="after")
    def validate_calibration(self) -> VendorRelativeConformalCalibration:
        if self.calibration_target_start_utc > self.calibration_target_end_utc:
            raise ValueError("calibration target start cannot follow target end")
        if len(set(self.calibration_pair_ids)) != len(self.calibration_pair_ids):
            raise ValueError("calibration_pair_ids cannot contain duplicates")
        labels = [group.label for group in self.groups]
        if len(set(labels)) != len(labels):
            raise ValueError("calibration group labels must be unique")
        if any(
            group.calibration_sample_count < self.minimum_calibration_samples
            for group in self.groups
        ):
            raise ValueError(
                "each calibration group must satisfy the minimum sample guard"
            )
        if self.method == "pooled_absolute_residual":
            if len(self.groups) != 1 or self.groups[0].horizon_band is not None:
                raise ValueError("pooled calibration requires one unconditioned group")
        elif any(group.horizon_band is None for group in self.groups):
            raise ValueError("horizon-conditioned groups require horizon bands")
        return self

    @property
    def nominal_coverage(self) -> float:
        return 1 - self.miscoverage


class VendorRelativePredictionInterval(BaseModel):
    """A symmetric interval evaluated against a later same-vendor value."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    pair_id: str
    geography: str
    target_valid_at_utc: datetime
    assumed_lead_hours: float = Field(ge=0)
    calibration_group: str
    forecast_temperature_c: float
    vendor_relative_realization_temperature_c: float
    lower_temperature_c: float
    upper_temperature_c: float

    @field_validator("target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return _require_utc_datetime(value, field_name=info.field_name)

    @field_serializer("target_valid_at_utc", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return _format_utc_datetime(value)

    @model_validator(mode="after")
    def validate_interval(self) -> VendorRelativePredictionInterval:
        if self.lower_temperature_c > self.upper_temperature_c:
            raise ValueError("interval lower bound cannot exceed upper bound")
        if not (
            self.lower_temperature_c
            <= self.forecast_temperature_c
            <= self.upper_temperature_c
        ):
            raise ValueError("interval must contain its point forecast")
        lower_radius = self.forecast_temperature_c - self.lower_temperature_c
        upper_radius = self.upper_temperature_c - self.forecast_temperature_c
        if not math.isclose(lower_radius, upper_radius, abs_tol=1e-12):
            raise ValueError("absolute-residual interval must be symmetric")
        return self

    @property
    def vendor_relative_residual_c(self) -> float:
        return (
            self.vendor_relative_realization_temperature_c - self.forecast_temperature_c
        )

    @property
    def contains_vendor_relative_realization(self) -> bool:
        return (
            self.lower_temperature_c
            <= self.vendor_relative_realization_temperature_c
            <= self.upper_temperature_c
        )


class VendorRelativeEvaluationMetrics(BaseModel):
    """Held-out reliability metrics against later same-vendor values."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    sample_count: int = Field(ge=1)
    empirical_interval_coverage: float = Field(ge=0, le=1)
    mean_interval_width_c: float = Field(ge=0)
    mean_absolute_vendor_relative_residual_c: float = Field(ge=0)
    mean_vendor_relative_residual_c: float
    screening_threshold_c: float
    vendor_relative_threshold_exceedance_count: int = Field(ge=0)
    point_forecast_threshold_miss_count: int = Field(ge=0)
    point_forecast_threshold_miss_rate: float | None = Field(default=None, ge=0, le=1)
    upper_interval_threshold_miss_count: int = Field(ge=0)
    upper_interval_threshold_miss_rate: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_counts_and_rates(self) -> VendorRelativeEvaluationMetrics:
        exceedances = self.vendor_relative_threshold_exceedance_count
        if exceedances > self.sample_count:
            raise ValueError("threshold exceedance count cannot exceed sample count")
        for label, count, rate in (
            (
                "point forecast",
                self.point_forecast_threshold_miss_count,
                self.point_forecast_threshold_miss_rate,
            ),
            (
                "upper interval",
                self.upper_interval_threshold_miss_count,
                self.upper_interval_threshold_miss_rate,
            ),
        ):
            if count > exceedances:
                raise ValueError(f"{label} miss count cannot exceed exceedance count")
            expected_rate = count / exceedances if exceedances else None
            if rate is None and expected_rate is not None:
                raise ValueError(
                    f"{label} miss rate is required when exceedances exist"
                )
            if rate is not None and (
                expected_rate is None
                or not math.isclose(rate, expected_rate, abs_tol=1e-12)
            ):
                raise ValueError(f"{label} miss rate must equal misses / exceedances")
        return self
