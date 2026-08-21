"""FortyGuard API integration boundary."""

from certiroute.fortyguard.client import ActivitySnapshot, FortyGuardClient
from certiroute.fortyguard.geometry import (
    DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    AoiCluster,
    bounding_polygon,
    cluster_points_into_aois,
    polygon_area_square_miles,
    validate_aoi_area,
)
from certiroute.fortyguard.results import TemperatureStats, extract_temperature_stats
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime

__all__ = [
    "ActivitySnapshot",
    "AoiCluster",
    "DEFAULT_MAX_AOI_AREA_SQUARE_MILES",
    "FortyGuardClient",
    "HeatmapRequest",
    "SingleHourDateTime",
    "TemperatureStats",
    "bounding_polygon",
    "cluster_points_into_aois",
    "extract_temperature_stats",
    "polygon_area_square_miles",
    "validate_aoi_area",
]
