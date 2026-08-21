"""Append-only forecast vintages and vendor-relative realization vintages."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from certiroute.collection._json import normalize_json_object
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    coerce_heatmap_request,
    forecast_record_id,
    heatmap_request_fingerprint,
    realization_record_id,
)
from certiroute.collection.models import (
    ForecastRecord,
    RequestTimeBasis,
    TileForecast,
    VendorRelativeRealizationRecord,
    VendorRelativeTileResidual,
    VendorRelativeTileValue,
    assumed_valid_at_utc,
    require_utc_datetime,
)
from certiroute.fortyguard.schemas import HeatmapRequest


class ForecastArchive:
    """Persist immutable forecast and later same-vendor comparison records.

    This class is passive: it neither schedules work nor performs network calls.
    Exact requests can have multiple issuance vintages, and callers must select a
    specific record ID or explicitly request the latest/listed vintage.
    """

    def __init__(self, root: str | Path) -> None:
        archive_root = Path(root)
        self._forecasts = JsonDiskCache(archive_root / "forecasts")
        self._realizations = JsonDiskCache(
            archive_root / "vendor_relative_realizations"
        )

    def fingerprint(self, request: HeatmapRequest | Mapping[str, Any]) -> str:
        """Return the normalized request fingerprint without touching disk."""

        return heatmap_request_fingerprint(request)

    def get_forecast(self, record_id: str) -> ForecastRecord | None:
        """Load one explicitly selected forecast issuance vintage."""

        payload = self._forecasts.get(record_id)
        if payload is None:
            return None
        try:
            record = ForecastRecord.model_validate(payload)
        except ValidationError as exc:
            raise CacheCorruptionError(
                f"invalid forecast record payload: {record_id}"
            ) from exc
        if record.record_id != record_id:
            raise CacheCorruptionError(
                "forecast record ID does not match its cache key"
            )
        return record

    def list_forecast_vintages(
        self,
        request: HeatmapRequest | Mapping[str, Any] | str,
    ) -> tuple[ForecastRecord, ...]:
        """List all vintages for a request/fingerprint, oldest issuance first."""

        fingerprint = _coerce_request_fingerprint(request)
        matches = [
            record
            for identifier in self._forecasts.record_ids()
            if (record := self.get_forecast(identifier)) is not None
            and record.request_fingerprint == fingerprint
        ]
        return tuple(
            sorted(matches, key=lambda item: (item.requested_at_utc, item.record_id))
        )

    def latest_forecast_for_request(
        self,
        request: HeatmapRequest | Mapping[str, Any] | str,
    ) -> ForecastRecord | None:
        """Explicitly select the most recently issued vintage for a request."""

        vintages = self.list_forecast_vintages(request)
        return vintages[-1] if vintages else None

    def record_forecast(
        self,
        request: HeatmapRequest | Mapping[str, Any],
        *,
        requested_at_utc: datetime,
        request_time_basis: RequestTimeBasis | Mapping[str, Any],
        activity_id: str,
        per_tile_forecasts: Sequence[TileForecast | Mapping[str, Any]] = (),
        raw_forecast_result: Mapping[str, Any] | None = None,
    ) -> ForecastRecord:
        """Append one completed forecast issuance without credentials or headers."""

        validated_request = coerce_heatmap_request(request)
        requested = require_utc_datetime(
            requested_at_utc, field_name="requested_at_utc"
        )
        basis = (
            request_time_basis
            if isinstance(request_time_basis, RequestTimeBasis)
            else RequestTimeBasis.model_validate(request_time_basis)
        )
        request_date = validated_request.date_time.start_date
        source_time = validated_request.date_time.start_time
        request_time = time(source_time.hour, source_time.minute)
        assumed_target = assumed_valid_at_utc(request_date, request_time, basis)
        if assumed_target < requested:
            raise ValueError("assumed forecast target cannot precede request issuance")
        fingerprint = heatmap_request_fingerprint(validated_request)
        identifier = forecast_record_id(fingerprint, requested, activity_id)
        forecasts = tuple(
            value
            if isinstance(value, TileForecast)
            else TileForecast.model_validate(value)
            for value in per_tile_forecasts
        )
        raw_result = (
            normalize_json_object(raw_forecast_result, path="$.raw_forecast_result")
            if raw_forecast_result is not None
            else None
        )
        record = ForecastRecord(
            record_id=identifier,
            request_fingerprint=fingerprint,
            requested_at_utc=requested,
            request_start_date=request_date,
            request_start_time=request_time,
            request_time_basis=basis,
            assumed_target_valid_at_utc=assumed_target,
            assumed_lead_hours=(assumed_target - requested).total_seconds() / 3600,
            aoi=_geometry_only_aoi(validated_request),
            granularity=validated_request.granularity,
            analytic_type=validated_request.analytic_type,
            activity_id=activity_id,
            per_tile_forecasts=forecasts,
            raw_forecast_result=raw_result,
        )
        self._forecasts.add(identifier, record.model_dump(mode="json"))
        return record

    def get_vendor_relative_realization(
        self, record_id: str
    ) -> VendorRelativeRealizationRecord | None:
        """Load one explicitly selected realization vintage."""

        payload = self._realizations.get(record_id)
        if payload is None:
            return None
        try:
            realization = VendorRelativeRealizationRecord.model_validate(payload)
        except ValidationError as exc:
            raise CacheCorruptionError(
                f"invalid vendor-relative realization payload: {record_id}"
            ) from exc
        if realization.record_id != record_id:
            raise CacheCorruptionError(
                "realization record ID does not match its cache key"
            )
        forecast = self.get_forecast(realization.forecast_record_id)
        if forecast is None:
            raise CacheCorruptionError("realization references a missing forecast")
        _validate_realization_against_forecast(realization, forecast)
        return realization

    def list_vendor_relative_realizations(
        self, forecast_id: str
    ) -> tuple[VendorRelativeRealizationRecord, ...]:
        """List append-only realization vintages for one forecast record."""

        _validate_identifier(forecast_id)
        matches = [
            record
            for identifier in self._realizations.record_ids()
            if (record := self.get_vendor_relative_realization(identifier)) is not None
            and record.forecast_record_id == forecast_id
        ]
        return tuple(
            sorted(matches, key=lambda item: (item.recorded_at_utc, item.record_id))
        )

    def latest_vendor_relative_realization(
        self, forecast_id: str
    ) -> VendorRelativeRealizationRecord | None:
        """Explicitly select the most recently recorded realization vintage."""

        vintages = self.list_vendor_relative_realizations(forecast_id)
        return vintages[-1] if vintages else None

    def record_vendor_relative_realization(
        self,
        forecast_id: str,
        *,
        request: HeatmapRequest | Mapping[str, Any],
        request_time_basis: RequestTimeBasis | Mapping[str, Any],
        recorded_at_utc: datetime,
        activity_id: str,
        per_tile_realizations: Sequence[VendorRelativeTileValue | Mapping[str, Any]],
        raw_result: Mapping[str, Any] | None = None,
    ) -> VendorRelativeRealizationRecord:
        """Append an exact-coverage, realization-minus-forecast comparison."""

        forecast = self.get_forecast(forecast_id)
        if forecast is None:
            raise KeyError(f"forecast not found: {forecast_id}")
        validated_request = coerce_heatmap_request(request)
        basis = (
            request_time_basis
            if isinstance(request_time_basis, RequestTimeBasis)
            else RequestTimeBasis.model_validate(request_time_basis)
        )
        request_fingerprint = heatmap_request_fingerprint(validated_request)
        safe_request = _geometry_only_request(validated_request)
        if request_fingerprint != forecast.request_fingerprint:
            raise ValueError(
                "realization request must match the selected forecast request"
            )
        if safe_request != _request_from_forecast(forecast):
            raise ValueError(
                "realization request target, AOI, granularity, or analytic type "
                "does not match the selected forecast"
            )
        if basis != forecast.request_time_basis:
            raise ValueError(
                "realization request time assumption must match the forecast"
            )
        recorded = require_utc_datetime(recorded_at_utc, field_name="recorded_at_utc")
        realized_values = tuple(
            value
            if isinstance(value, VendorRelativeTileValue)
            else VendorRelativeTileValue.model_validate(value)
            for value in per_tile_realizations
        )
        realized_keys = [value.spatial_key for value in realized_values]
        if len(set(realized_keys)) != len(realized_keys):
            raise ValueError(
                "per_tile_realizations contains duplicate spatial geometry"
            )

        forecasts_by_key = {
            item.spatial_key: item for item in forecast.per_tile_forecasts
        }
        realized_by_key = {item.spatial_key: item for item in realized_values}
        missing = sorted(forecasts_by_key.keys() - realized_by_key.keys())
        unexpected = sorted(realized_by_key.keys() - forecasts_by_key.keys())
        if missing or unexpected:
            details = []
            if missing:
                details.append(f"missing={','.join(missing)}")
            if unexpected:
                details.append(f"unexpected={','.join(unexpected)}")
            raise ValueError(
                "vendor-relative realization must exactly cover forecast geometry ("
                + "; ".join(details)
                + ")"
            )
        if not forecasts_by_key:
            raise ValueError(
                "vendor-relative residuals require spatial forecast tile data"
            )

        residuals = tuple(
            _tile_residual(forecast_tile, realized_by_key[forecast_tile.spatial_key])
            for forecast_tile in forecast.per_tile_forecasts
        )
        identifier = realization_record_id(forecast_id, recorded, activity_id)
        realization = VendorRelativeRealizationRecord(
            record_id=identifier,
            forecast_record_id=forecast_id,
            request_fingerprint=forecast.request_fingerprint,
            realization_request_fingerprint=request_fingerprint,
            realization_request=safe_request,
            realization_request_time_basis=basis,
            recorded_at_utc=recorded,
            forecast_assumed_target_valid_at_utc=(forecast.assumed_target_valid_at_utc),
            activity_id=activity_id,
            per_tile_residuals=residuals,
            mean_vendor_relative_residual_c=(
                sum(item.vendor_relative_residual_c for item in residuals)
                / len(residuals)
            ),
            raw_result=(
                normalize_json_object(raw_result, path="$.raw_result")
                if raw_result is not None
                else None
            ),
        )
        self._realizations.add(identifier, realization.model_dump(mode="json"))
        return realization


def _tile_residual(
    forecast: TileForecast,
    realization: VendorRelativeTileValue,
) -> VendorRelativeTileResidual:
    realized_temperature = realization.vendor_relative_realization_temperature_c
    return VendorRelativeTileResidual(
        spatial_key=forecast.spatial_key,
        forecast_temperature_c=forecast.forecast_temperature_c,
        vendor_relative_realization_temperature_c=realized_temperature,
        vendor_relative_residual_c=(
            realized_temperature - forecast.forecast_temperature_c
        ),
        forecast_vendor_tile_id=forecast.vendor_tile_id,
        realization_vendor_tile_id=realization.vendor_tile_id,
        realization_tile_data=realization.tile_data,
    )


def _validate_realization_against_forecast(
    realization: VendorRelativeRealizationRecord,
    forecast: ForecastRecord,
) -> None:
    if realization.request_fingerprint != forecast.request_fingerprint:
        raise CacheCorruptionError("realization request fingerprint is inconsistent")
    if realization.realization_request_fingerprint != forecast.request_fingerprint:
        raise CacheCorruptionError(
            "realization source request fingerprint is inconsistent"
        )
    if realization.realization_request != _request_from_forecast(forecast):
        raise CacheCorruptionError("realization source request is inconsistent")
    if realization.realization_request_time_basis != forecast.request_time_basis:
        raise CacheCorruptionError(
            "realization request time assumption is inconsistent"
        )
    if (
        realization.forecast_assumed_target_valid_at_utc
        != forecast.assumed_target_valid_at_utc
    ):
        raise CacheCorruptionError("realization target is inconsistent")
    forecast_keys = {item.spatial_key for item in forecast.per_tile_forecasts}
    realized_keys = {item.spatial_key for item in realization.per_tile_residuals}
    if forecast_keys != realized_keys:
        raise CacheCorruptionError("realization spatial coverage is inconsistent")
    forecasts_by_key = {item.spatial_key: item for item in forecast.per_tile_forecasts}
    for residual in realization.per_tile_residuals:
        forecast_tile = forecasts_by_key[residual.spatial_key]
        if residual.forecast_temperature_c != forecast_tile.forecast_temperature_c:
            raise CacheCorruptionError(
                "realization forecast temperature is inconsistent"
            )
        if residual.forecast_vendor_tile_id != forecast_tile.vendor_tile_id:
            raise CacheCorruptionError("realization forecast tile ID is inconsistent")


def _geometry_only_aoi(request: HeatmapRequest) -> dict[str, Any]:
    """Persist only GeoJSON geometry; arbitrary feature properties are omitted."""

    return {
        "type": request.polygon_aoi.type,
        "features": [
            {
                "type": feature.type,
                "geometry": feature.geometry.model_dump(mode="json"),
            }
            for feature in request.polygon_aoi.features
        ],
    }


def _geometry_only_request(request: HeatmapRequest) -> dict[str, Any]:
    """Persist request identity without arbitrary GeoJSON feature properties."""

    return {
        "polygon_aoi": _geometry_only_aoi(request),
        "date_time": request.date_time.model_dump(mode="json"),
        "granularity": request.granularity,
        "analytic_type": request.analytic_type,
    }


def _request_from_forecast(forecast: ForecastRecord) -> dict[str, Any]:
    """Rebuild the safe request fields committed by a forecast record."""

    return {
        "polygon_aoi": forecast.aoi,
        "date_time": {
            "start_date": forecast.request_start_date.isoformat(),
            "start_time": forecast.request_start_time.strftime("%H:%M"),
            "filter_type": 1,
        },
        "granularity": forecast.granularity,
        "analytic_type": forecast.analytic_type,
    }


def _coerce_request_fingerprint(
    request: HeatmapRequest | Mapping[str, Any] | str,
) -> str:
    if isinstance(request, str):
        return _validate_identifier(request)
    return heatmap_request_fingerprint(request)


def _validate_identifier(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("identifier must be 64 lowercase hex characters")
    return value
