"""Immutable forecast-vintage and vendor-relative realization schemas."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, date, datetime, time, timedelta, timezone
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

from certiroute.collection._json import normalize_json_object
from certiroute.collection.fingerprints import (
    forecast_record_id,
    realization_record_id,
)
from certiroute.collection.spatial import (
    canonical_tile_geometry,
    tile_spatial_key,
)


class RequestTimeBasis(BaseModel):
    """Caller's explicit assumption for the API request's naive wall clock.

    FortyGuard's request-time timezone semantics are undocumented. This model
    records a fixed offset used for this request; it does not claim that the
    vendor confirmed that interpretation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["caller_supplied_assumption"] = "caller_supplied_assumption"
    assumption: str = Field(min_length=1, max_length=500)
    utc_offset_minutes: int = Field(ge=-14 * 60, le=14 * 60)

    @field_validator("assumption")
    @classmethod
    def normalize_assumption(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("assumption cannot be blank")
        return normalized


class TileForecast(BaseModel):
    """A forecast tile keyed by canonical geometry, not a vendor tile ID."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    spatial_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry: dict[str, Any]
    forecast_temperature_c: float
    vendor_tile_id: str | None = None
    tile_data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def derive_spatial_identity(cls, value: Any) -> Any:
        return _with_spatial_identity(value, path="$.per_tile_forecasts[]")

    @field_validator("vendor_tile_id")
    @classmethod
    def normalize_vendor_tile_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.tile_data")


class VendorRelativeTileValue(BaseModel):
    """A later same-vendor value identified by canonical tile geometry."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    spatial_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    geometry: dict[str, Any]
    vendor_relative_realization_temperature_c: float
    vendor_tile_id: str | None = None
    tile_data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def derive_spatial_identity(cls, value: Any) -> Any:
        return _with_spatial_identity(value, path="$.per_tile_realizations[]")

    @field_validator("vendor_tile_id")
    @classmethod
    def normalize_vendor_tile_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.tile_data")


class VendorRelativeTileResidual(BaseModel):
    """Later-vendor value minus forecast for one canonical spatial tile."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    spatial_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_temperature_c: float
    vendor_relative_realization_temperature_c: float
    vendor_relative_residual_c: float
    forecast_vendor_tile_id: str | None = None
    realization_vendor_tile_id: str | None = None
    realization_tile_data: dict[str, Any] = Field(default_factory=dict)

    @field_validator("realization_tile_data", mode="before")
    @classmethod
    def validate_tile_data(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.realization_tile_data")

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


class ForecastRecord(BaseModel):
    """One append-only issuance vintage for a normalized heatmap request."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    record_schema_version: Literal[2] = 2
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_at_utc: datetime
    request_start_date: date
    request_start_time: time
    request_time_basis: RequestTimeBasis
    assumed_target_valid_at_utc: datetime
    assumed_lead_hours: float = Field(ge=0)
    aoi: dict[str, Any]
    granularity: Literal[60, 80, 100]
    analytic_type: Literal["tcm"] = "tcm"
    activity_id: str = Field(min_length=1)
    per_tile_forecasts: tuple[TileForecast, ...] = ()
    raw_forecast_result: dict[str, Any] | None = None

    @field_validator("requested_at_utc", "assumed_target_valid_at_utc", mode="before")
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "requested_at_utc", "assumed_target_valid_at_utc", when_used="json"
    )
    def serialize_timestamp(self, value: datetime) -> str:
        return format_utc_datetime(value)

    @field_validator("request_start_time")
    @classmethod
    def validate_request_time(cls, value: time) -> time:
        if value.tzinfo is not None:
            raise ValueError("request_start_time must be an unzoned wall-clock time")
        if value.second or value.microsecond:
            raise ValueError("request_start_time precision is one minute")
        return value

    @field_serializer("request_start_time", when_used="json")
    def serialize_request_time(self, value: time) -> str:
        return value.strftime("%H:%M")

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
        expected_target = assumed_valid_at_utc(
            self.request_start_date,
            self.request_start_time,
            self.request_time_basis,
        )
        if self.assumed_target_valid_at_utc != expected_target:
            raise ValueError(
                "assumed_target_valid_at_utc does not match request wall clock "
                "and caller-supplied offset"
            )
        expected_lead = (
            self.assumed_target_valid_at_utc - self.requested_at_utc
        ).total_seconds() / 3600
        if expected_lead < 0:
            raise ValueError("assumed forecast target cannot precede request issuance")
        if not math.isclose(self.assumed_lead_hours, expected_lead, abs_tol=1e-9):
            raise ValueError("assumed_lead_hours does not match the timestamps")
        identifiers = [item.spatial_key for item in self.per_tile_forecasts]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("per_tile_forecasts contains duplicate spatial geometry")
        if not self.per_tile_forecasts and self.raw_forecast_result is None:
            raise ValueError("a forecast record needs tile data or a raw result")
        expected_id = forecast_record_id(
            self.request_fingerprint, self.requested_at_utc, self.activity_id
        )
        if self.record_id != expected_id:
            raise ValueError("record_id does not match the forecast vintage identity")
        return self


class VendorRelativeRealizationRecord(BaseModel):
    """One append-only later-vendor comparison for a forecast vintage."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    record_schema_version: Literal[1] = 1
    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    forecast_record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    realization_request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    realization_request: dict[str, Any]
    realization_request_time_basis: RequestTimeBasis
    residual_definition: Literal["vendor_relative_realization_minus_forecast"] = (
        "vendor_relative_realization_minus_forecast"
    )
    recorded_at_utc: datetime
    forecast_assumed_target_valid_at_utc: datetime
    activity_id: str = Field(min_length=1)
    per_tile_residuals: tuple[VendorRelativeTileResidual, ...] = Field(min_length=1)
    mean_vendor_relative_residual_c: float
    raw_result: dict[str, Any] | None = None

    @field_validator(
        "recorded_at_utc", "forecast_assumed_target_valid_at_utc", mode="before"
    )
    @classmethod
    def normalize_timestamp(cls, value: Any, info: ValidationInfo) -> datetime:
        return require_utc_datetime(value, field_name=info.field_name)

    @field_serializer(
        "recorded_at_utc", "forecast_assumed_target_valid_at_utc", when_used="json"
    )
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

    @field_validator("realization_request", mode="before")
    @classmethod
    def validate_realization_request(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.realization_request")

    @model_validator(mode="after")
    def validate_consistency(self) -> VendorRelativeRealizationRecord:
        identifiers = [item.spatial_key for item in self.per_tile_residuals]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("per_tile_residuals contains duplicate spatial keys")
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
        if self.recorded_at_utc < self.forecast_assumed_target_valid_at_utc:
            raise ValueError("recorded_at_utc cannot precede the assumed target")
        if self.realization_request_fingerprint != self.request_fingerprint:
            raise ValueError(
                "realization request fingerprint must match the forecast request"
            )
        expected_id = realization_record_id(
            self.forecast_record_id, self.recorded_at_utc, self.activity_id
        )
        if self.record_id != expected_id:
            raise ValueError("record_id does not match the realization identity")
        return self


def assumed_valid_at_utc(
    start_date: date,
    start_time: time,
    basis: RequestTimeBasis,
) -> datetime:
    """Apply an explicitly assumed fixed offset to an API wall-clock target."""

    wall_clock = datetime.combine(
        start_date,
        time(start_time.hour, start_time.minute),
    )
    assumed_zone = timezone(timedelta(minutes=basis.utc_offset_minutes))
    return wall_clock.replace(tzinfo=assumed_zone).astimezone(UTC)


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


def _with_spatial_identity(value: Any, *, path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be a JSON object")
    data = normalize_json_object(value, path=path)
    geometry_value = data.get("geometry")
    if not isinstance(geometry_value, Mapping):
        raise TypeError(f"{path}.geometry must be a JSON object")
    geometry = canonical_tile_geometry(geometry_value)
    expected_key = tile_spatial_key(geometry)
    supplied_key = data.get("spatial_key")
    if supplied_key is not None and supplied_key != expected_key:
        raise ValueError(f"{path}.spatial_key does not match canonical geometry")
    data["geometry"] = geometry
    data["spatial_key"] = expected_key
    return data
