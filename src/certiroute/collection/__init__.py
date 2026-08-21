"""Secret-free forecast caching and vendor-relative residual collection."""

from certiroute.collection._json import UnsafeCachePayloadError
from certiroute.collection.archive import ForecastArchive
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    canonical_heatmap_request_json,
    forecast_record_id,
    heatmap_request_fingerprint,
    normalize_heatmap_request,
    realization_record_id,
)
from certiroute.collection.models import (
    ForecastRecord,
    RequestTimeBasis,
    TileForecast,
    VendorRelativeRealizationRecord,
    VendorRelativeTileResidual,
    VendorRelativeTileValue,
)
from certiroute.collection.snapshot_cache import (
    DEFAULT_SNAPSHOT_CACHE_ROOT,
    HeatmapSnapshot,
    HeatmapSnapshotStore,
    SnapshotTemporalScope,
    heatmap_snapshot_id,
)
from certiroute.collection.spatial import canonical_tile_geometry, tile_spatial_key

__all__ = [
    "CacheCorruptionError",
    "DEFAULT_SNAPSHOT_CACHE_ROOT",
    "ForecastArchive",
    "ForecastRecord",
    "HeatmapSnapshot",
    "HeatmapSnapshotStore",
    "JsonDiskCache",
    "RequestTimeBasis",
    "SnapshotTemporalScope",
    "TileForecast",
    "UnsafeCachePayloadError",
    "VendorRelativeRealizationRecord",
    "VendorRelativeTileResidual",
    "VendorRelativeTileValue",
    "canonical_heatmap_request_json",
    "canonical_tile_geometry",
    "forecast_record_id",
    "heatmap_request_fingerprint",
    "heatmap_snapshot_id",
    "normalize_heatmap_request",
    "realization_record_id",
    "tile_spatial_key",
]
