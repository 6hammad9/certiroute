"""Secret-free forecast caching and vendor-relative residual collection."""

from certiroute.collection._json import UnsafeCachePayloadError
from certiroute.collection.archive import ForecastArchive
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    canonical_heatmap_request_json,
    heatmap_request_fingerprint,
    normalize_heatmap_request,
)
from certiroute.collection.models import (
    ForecastRecord,
    TileForecast,
    VendorRelativeRealization,
    VendorRelativeTileResidual,
    VendorRelativeTileValue,
)

__all__ = [
    "CacheCorruptionError",
    "ForecastArchive",
    "ForecastRecord",
    "JsonDiskCache",
    "TileForecast",
    "UnsafeCachePayloadError",
    "VendorRelativeRealization",
    "VendorRelativeTileResidual",
    "VendorRelativeTileValue",
    "canonical_heatmap_request_json",
    "heatmap_request_fingerprint",
    "normalize_heatmap_request",
]
