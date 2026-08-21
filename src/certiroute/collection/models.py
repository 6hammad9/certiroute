"""Persistent forecast and vendor-relative residual record schemas."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from certiroute.collection._json import normalize_json_object


class TileForecast(BaseModel):
    """A stable tile identifier and its temperature forecast."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    tile_id: str = Field(min_length=1)
    forecast_temperature_c: float
    tile_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tile_id")
    @classmethod
    def normalize_tile_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tile_id cannot be blank")
        return normalized

    @field_validator("tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.tile_data")


class VendorRelativeTileValue(BaseModel):
    """A later value from the same vendor for one forecast tile."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    tile_id: str = Field(min_length=1)
    vendor_relative_realization_temperature_c: float
    tile_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tile_id")
    @classmethod
    def normalize_tile_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("tile_id cannot be blank")
        return normalized

    @field_validator("tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.tile_data")


class VendorRelativeTileResidual(BaseModel):
    """Residual for one tile, defined as later vendor value minus forecast."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    tile_id: str = Field(min_length=1)
    forecast_temperature_c: float
    vendor_relative_realization_temperature_c: float
    vendor_relative_residual_c: float
    tile_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.tile_data")

    @model_validator(mode="after")
    def validate_residual(self) -> VendorRelativeTileResidual:
        expected = (
            self.vendor_relative_realization_temperature_c - self.forecast_temperature_c
        )
        if not math.isclose(
            self.vendor_relative_residual_c, expected, rel_tol=1e-12, abs_tol=1e-12
        ):
            raise ValueError(
                "vendor_relative_residual_c must equal realization minus forecast"
            )
        return self


class VendorRelativeRealization(BaseModel):
    """A later FortyGuard result used as a vendor-relative comparison target.

    This record does not assert that the vendor's later value is an independent
    physical observation. It is suitable for vendor-relative backtesting only.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    residual_definition: Literal["vendor_relative_realization_minus_forecast"] = (
        "vendor_relative_realization_minus_forecast"
    )
    recorded_at_utc: datetime
    target_valid_at_utc: datetime
    activity_id: str = Field(min_length=1)
    per_tile_residuals: tuple[VendorRelativeTileResidual, ...] = Field(min_length=1)
    mean_vendor_relative_residual_c: float
    raw_result: dict[str, Any] | None = None

    @field_validator("recorded_at_utc", "target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: Any) -> datetime:
        return require_utc_datetime(value, field_name=info.field_name)

    @field_serializer("recorded_at_utc", "target_valid_at_utc", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return format_utc_datetime(value)

    @field_validator("activity_id")
    @classmethod
    def normalize_activity_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("activity_id cannot be blank")
        return normalized

    @field_validator("raw_result", mode="before")
    @classmethod
    def validate_raw_result(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return normalize_json_object(value, path="$.raw_result")

    @model_validator(mode="after")
    def validate_consistency(self) -> VendorRelativeRealization:
        identifiers = [item.tile_id for item in self.per_tile_residuals]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("per_tile_residuals contains duplicate tile_id values")
        expected_mean = sum(
            item.vendor_relative_residual_c for item in self.per_tile_residuals
        ) / len(self.per_tile_residuals)
        if not math.isclose(
            self.mean_vendor_relative_residual_c,
            expected_mean,
            rel_tol=1e-12,
            abs_tol=1e-12,
        ):
            raise ValueError(
                "mean_vendor_relative_residual_c must equal the tile residual mean"
            )
        if self.recorded_at_utc < self.target_valid_at_utc:
            raise ValueError("recorded_at_utc cannot precede target_valid_at_utc")
        return self


class ForecastRecord(BaseModel):
    """Immutable cache record for one distinct normalized heatmap request."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    record_schema_version: Literal[1] = 1
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at_utc: datetime
    target_valid_at_utc: datetime
    lead_hours: float = Field(ge=0)
    aoi: dict[str, Any]
    granularity: Literal[60, 80, 100]
    analytic_type: Literal["tcm"] = "tcm"
    activity_id: str = Field(min_length=1)
    per_tile_forecasts: tuple[TileForecast, ...] = ()
    raw_forecast_result: dict[str, Any] | None = None
    vendor_relative_realization: VendorRelativeRealization | None = None

    @field_validator("requested_at_utc", "target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: Any) -> datetime:
        return require_utc_datetime(value, field_name=info.field_name)

    @field_serializer("requested_at_utc", "target_valid_at_utc", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return format_utc_datetime(value)

    @field_validator("activity_id")
    @classmethod
    def normalize_activity_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("activity_id cannot be blank")
        return normalized

    @field_validator("aoi", mode="before")
    @classmethod
    def validate_aoi(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.aoi")

    @field_validator("raw_forecast_result", mode="before")
    @classmethod
    def validate_raw_result(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        return normalize_json_object(value, path="$.raw_forecast_result")

    @model_validator(mode="after")
    def validate_consistency(self) -> ForecastRecord:
        if self.target_valid_at_utc < self.requested_at_utc:
            raise ValueError("target_valid_at_utc cannot precede requested_at_utc")
        expected_lead = (
            self.target_valid_at_utc - self.requested_at_utc
        ).total_seconds() / 3600
        if not math.isclose(self.lead_hours, expected_lead, abs_tol=1e-9):
            raise ValueError("lead_hours must match the two UTC timestamps")
        identifiers = [item.tile_id for item in self.per_tile_forecasts]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("per_tile_forecasts contains duplicate tile_id values")
        if not self.per_tile_forecasts and self.raw_forecast_result is None:
            raise ValueError("a forecast record needs tile data or a raw result")
        realization = self.vendor_relative_realization
        if realization is not None:
            if realization.target_valid_at_utc != self.target_valid_at_utc:
                raise ValueError("realization target does not match forecast target")
            if realization.recorded_at_utc < self.target_valid_at_utc:
                raise ValueError("realization cannot precede the forecast target")
        return self


def require_utc_datetime(value: Any, *, field_name: str) -> datetime:
    """Parse an aware datetime and normalize it to UTC."""

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


def format_utc_datetime(value: datetime) -> str:
    """Serialize UTC timestamps with an unambiguous ``Z`` suffix."""

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
