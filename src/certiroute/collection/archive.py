"""Forecast cache and later vendor-relative realization collection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from certiroute.collection._json import normalize_json_object
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    coerce_heatmap_request,
    heatmap_request_fingerprint,
)
from certiroute.collection.models import (
    ForecastRecord,
    TileForecast,
    VendorRelativeRealization,
    VendorRelativeTileResidual,
    VendorRelativeTileValue,
    require_utc_datetime,
)
from certiroute.fortyguard.schemas import HeatmapRequest


class ForecastArchive:
    """Persist forecasts and enrich them later with vendor-relative residuals.

    This class is deliberately passive: it neither schedules work nor performs
    network requests. Callers submit already-completed API results.
    """

    def __init__(self, root: str | Path) -> None:
        self._cache = JsonDiskCache(root)

    def fingerprint(self, request: HeatmapRequest | Mapping[str, Any]) -> str:
        """Return the cache key without reading or writing the cache."""

        return heatmap_request_fingerprint(request)

    def get(self, request_fingerprint: str) -> ForecastRecord | None:
        """Load a forecast record by fingerprint."""

        payload = self._cache.get(request_fingerprint)
        if payload is None:
            return None
        record = ForecastRecord.model_validate(payload)
        if record.request_fingerprint != request_fingerprint:
            raise CacheCorruptionError(
                "forecast record fingerprint does not match its cache key"
            )
        return record

    def get_for_request(
        self, request: HeatmapRequest | Mapping[str, Any]
    ) -> ForecastRecord | None:
        """Look up an exact normalized request, suitable for pre-call caching."""

        return self.get(heatmap_request_fingerprint(request))

    def record_forecast(
        self,
        request: HeatmapRequest | Mapping[str, Any],
        *,
        requested_at_utc: datetime,
        target_valid_at_utc: datetime,
        activity_id: str,
        per_tile_forecasts: Sequence[TileForecast | Mapping[str, Any]] = (),
        raw_forecast_result: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> ForecastRecord:
        """Store one completed forecast without retaining headers or API keys."""

        validated_request = coerce_heatmap_request(request)
        requested = require_utc_datetime(
            requested_at_utc, field_name="requested_at_utc"
        )
        target = require_utc_datetime(
            target_valid_at_utc, field_name="target_valid_at_utc"
        )
        fingerprint = heatmap_request_fingerprint(validated_request)
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
            request_fingerprint=fingerprint,
            requested_at_utc=requested,
            target_valid_at_utc=target,
            lead_hours=(target - requested).total_seconds() / 3600,
            aoi=_geometry_only_aoi(validated_request),
            granularity=validated_request.granularity,
            analytic_type=validated_request.analytic_type,
            activity_id=activity_id,
            per_tile_forecasts=forecasts,
            raw_forecast_result=raw_result,
        )
        self._cache.put(
            fingerprint,
            record.model_dump(mode="json"),
            overwrite=overwrite,
        )
        return record

    def record_vendor_relative_realization(
        self,
        request_fingerprint: str,
        *,
        recorded_at_utc: datetime,
        activity_id: str,
        per_tile_realizations: Sequence[VendorRelativeTileValue | Mapping[str, Any]],
        raw_result: Mapping[str, Any] | None = None,
        overwrite: bool = False,
    ) -> ForecastRecord:
        """Attach later same-vendor values and realization-minus-forecast residuals."""

        record = self.get(request_fingerprint)
        if record is None:
            raise KeyError(f"forecast not found: {request_fingerprint}")
        if record.vendor_relative_realization is not None and not overwrite:
            raise FileExistsError(
                f"vendor-relative realization already exists: {request_fingerprint}"
            )

        realized_values = tuple(
            value
            if isinstance(value, VendorRelativeTileValue)
            else VendorRelativeTileValue.model_validate(value)
            for value in per_tile_realizations
        )
        if not realized_values:
            raise ValueError("per_tile_realizations must contain at least one tile")
        realized_ids = [value.tile_id for value in realized_values]
        if len(set(realized_ids)) != len(realized_ids):
            raise ValueError("per_tile_realizations contains duplicate tile_id values")

        forecasts_by_id = {
            forecast.tile_id: forecast for forecast in record.per_tile_forecasts
        }
        unknown = sorted(set(realized_ids) - forecasts_by_id.keys())
        if unknown:
            raise ValueError(
                "vendor-relative values do not match forecast tiles: "
                + ", ".join(unknown)
            )

        residuals = tuple(
            VendorRelativeTileResidual(
                tile_id=value.tile_id,
                forecast_temperature_c=forecasts_by_id[
                    value.tile_id
                ].forecast_temperature_c,
                vendor_relative_realization_temperature_c=(
                    value.vendor_relative_realization_temperature_c
                ),
                vendor_relative_residual_c=(
                    value.vendor_relative_realization_temperature_c
                    - forecasts_by_id[value.tile_id].forecast_temperature_c
                ),
                tile_data=value.tile_data,
            )
            for value in realized_values
        )
        mean_residual = sum(
            value.vendor_relative_residual_c for value in residuals
        ) / len(residuals)
        realization = VendorRelativeRealization(
            recorded_at_utc=recorded_at_utc,
            target_valid_at_utc=record.target_valid_at_utc,
            activity_id=activity_id,
            per_tile_residuals=residuals,
            mean_vendor_relative_residual_c=mean_residual,
            raw_result=(
                normalize_json_object(raw_result, path="$.raw_result")
                if raw_result is not None
                else None
            ),
        )
        updated_payload = record.model_dump(mode="json")
        updated_payload["vendor_relative_realization"] = realization.model_dump(
            mode="json"
        )
        updated = ForecastRecord.model_validate(updated_payload)
        self._cache.put(
            request_fingerprint,
            updated.model_dump(mode="json"),
            overwrite=True,
        )
        return updated


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
