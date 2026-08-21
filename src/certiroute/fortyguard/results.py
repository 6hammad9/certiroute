"""Helpers for extracting stable summaries from endpoint-specific results."""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict


class TemperatureStats(BaseModel):
    """Normalized aggregate values returned by a completed heatmap."""

    model_config = ConfigDict(frozen=True)

    minimum_c: float | None = None
    maximum_c: float | None = None
    mean_c: float | None = None
    standard_deviation_c: float | None = None


def extract_temperature_stats(result: Mapping[str, Any]) -> TemperatureStats:
    """Normalize documented statistic keys without depending on capitalization."""

    stats_data = _mapping_value(result, "stats_data")
    temperature_stats = _mapping_value(stats_data, "temperature_stats")
    return TemperatureStats(
        minimum_c=_float_value(temperature_stats, "minimum", "min"),
        maximum_c=_float_value(temperature_stats, "maximum", "max"),
        mean_c=_float_value(temperature_stats, "mean", "average"),
        standard_deviation_c=_float_value(
            temperature_stats, "standard_deviation", "std", "std_dev"
        ),
    )


def _mapping_value(mapping: Mapping[str, Any], wanted_key: str) -> Mapping[str, Any]:
    wanted = _normalize_key(wanted_key)
    for key, value in mapping.items():
        if _normalize_key(str(key)) == wanted and isinstance(value, Mapping):
            return value
    return {}


def _float_value(mapping: Mapping[str, Any], *keys: str) -> float | None:
    wanted = {_normalize_key(key) for key in keys}
    for key, value in mapping.items():
        if _normalize_key(str(key)) not in wanted:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _normalize_key(key: str) -> str:
    return key.strip().lower().replace("-", "_").replace(" ", "_")
