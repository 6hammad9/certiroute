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
from certiroute.fortyguard.heatmap_profiles import (
    UNCALIBRATED_CERTAINTY,
    HeatmapCoverageError,
    HeatmapTile,
    build_temperature_profiles,
    extract_heatmap_tiles,
    geometry_covers_point,
    map_job_temperatures,
)
from certiroute.fortyguard.results import TemperatureStats, extract_temperature_stats
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime

__all__ = [
    "ActivitySnapshot",
    "AoiCluster",
    "DEFAULT_MAX_AOI_AREA_SQUARE_MILES",
    "FortyGuardClient",
    "HeatmapRequest",
    "HeatmapCoverageError",
    "HeatmapTile",
    "SingleHourDateTime",
    "TemperatureStats",
    "UNCALIBRATED_CERTAINTY",
    "bounding_polygon",
    "build_temperature_profiles",
    "cluster_points_into_aois",
    "extract_temperature_stats",
    "extract_heatmap_tiles",
    "geometry_covers_point",
    "map_job_temperatures",
    "polygon_area_square_miles",
    "validate_aoi_area",
]
