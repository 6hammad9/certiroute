"""FortyGuard API integration boundary."""

from certiroute.fortyguard.client import ActivitySnapshot, FortyGuardClient
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.fortyguard.results import TemperatureStats, extract_temperature_stats
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime

__all__ = [
    "ActivitySnapshot",
    "FortyGuardClient",
    "HeatmapRequest",
    "SingleHourDateTime",
    "TemperatureStats",
    "bounding_polygon",
    "extract_temperature_stats",
]
