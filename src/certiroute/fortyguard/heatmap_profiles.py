"""Turn completed FortyGuard heatmaps into per-job temperature profiles.

The optimizer currently requires a numeric ``certainty`` at every condition
point. FortyGuard's heatmap response does not provide a calibrated probability,
so live profiles use ``UNCALIBRATED_CERTAINTY`` as a computationally neutral
sentinel: with the current risk formula, ``1.0`` applies no uncertainty penalty.
It must not be displayed or interpreted as 100% forecast confidence.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import isfinite
from typing import Any, Literal, TypeAlias

from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard.errors import FortyGuardProtocolError
from certiroute.optimization import ConditionPoint, TemperatureProfile

UNCALIBRATED_CERTAINTY = 1.0

Position: TypeAlias = tuple[float, float]
LinearRing: TypeAlias = tuple[Position, ...]
PolygonRings: TypeAlias = tuple[LinearRing, ...]
ParsedPolygons: TypeAlias = tuple[PolygonRings, ...]


class HeatmapCoverageError(FortyGuardProtocolError):
    """A job is uncovered by, or ambiguously covered by, heatmap tiles."""


@dataclass(frozen=True)
class HeatmapTile:
    """One validated temperature tile from ``result.map_data.features``."""

    feature_index: int
    geometry_type: Literal["Polygon", "MultiPolygon"]
    polygons: ParsedPolygons
    average_temperature_c: float
    vendor_tile_id: str | None = None

    def covers(self, point: GeoPoint) -> bool:
        """Return whether this tile covers a point, including its boundaries."""

        position = point.geojson_position
        return any(_polygon_covers(position, polygon) for polygon in self.polygons)


def extract_heatmap_tiles(result: Mapping[str, Any]) -> tuple[HeatmapTile, ...]:
    """Parse all Polygon/MultiPolygon tiles from one completed API result.

    ``average_temperature`` must be an actual finite JSON number. Malformed or
    incomplete results fail at this boundary instead of becoming partial input
    to the scheduler.
    """

    map_data = _required_mapping(result, "map_data", path="result")
    if map_data.get("type") not in (None, "FeatureCollection"):
        raise FortyGuardProtocolError(
            "result.map_data.type must be 'FeatureCollection'"
        )
    features = map_data.get("features")
    if not _is_sequence(features) or not features:
        raise FortyGuardProtocolError(
            "result.map_data.features must contain at least one feature"
        )

    tiles: list[HeatmapTile] = []
    for index, raw_feature in enumerate(features):
        path = f"result.map_data.features[{index}]"
        if not isinstance(raw_feature, Mapping):
            raise FortyGuardProtocolError(f"{path} must be an object")
        if raw_feature.get("type") not in (None, "Feature"):
            raise FortyGuardProtocolError(f"{path}.type must be 'Feature'")
        geometry = _required_mapping(raw_feature, "geometry", path=path)
        geometry_type, polygons = _parse_geometry(geometry, path=f"{path}.geometry")
        properties = _required_mapping(raw_feature, "properties", path=path)
        temperature = _finite_number(
            properties.get("average_temperature"),
            path=f"{path}.properties.average_temperature",
        )
        raw_tile_id = properties.get("tile_id")
        vendor_tile_id = raw_tile_id.strip() if isinstance(raw_tile_id, str) else None
        tiles.append(
            HeatmapTile(
                feature_index=index,
                geometry_type=geometry_type,
                polygons=polygons,
                average_temperature_c=temperature,
                vendor_tile_id=vendor_tile_id or None,
            )
        )
    return tuple(tiles)


def geometry_covers_point(geometry: Mapping[str, Any], point: GeoPoint) -> bool:
    """Test Polygon/MultiPolygon coverage with GeoJSON hole semantics.

    Exterior and interior-ring boundaries are part of the polygon boundary and
    therefore count as covered. The interior of a hole does not count.
    """

    _, polygons = _parse_geometry(geometry, path="geometry")
    position = point.geojson_position
    return any(_polygon_covers(position, polygon) for polygon in polygons)


def map_job_temperatures(
    jobs: Sequence[Job], result: Mapping[str, Any]
) -> dict[str, float]:
    """Map every job to exactly one heatmap tile or raise a clear error.

    A point on a shared tile edge may be covered by multiple polygons. If those
    matches disagree on temperature, selecting one by response order would be
    arbitrary, so the result is rejected. Identical-temperature overlaps are
    harmless and collapse to that shared value.
    """

    _validate_unique_job_ids(jobs)
    tiles = extract_heatmap_tiles(result)
    temperatures: dict[str, float] = {}
    uncovered: list[str] = []
    ambiguous: list[str] = []

    for job in jobs:
        matches = [tile for tile in tiles if tile.covers(job.location)]
        if not matches:
            uncovered.append(job.job_id)
            continue
        distinct_temperatures = {tile.average_temperature_c for tile in matches}
        if len(distinct_temperatures) > 1:
            ambiguous.append(job.job_id)
            continue
        temperatures[job.job_id] = matches[0].average_temperature_c

    problems: list[str] = []
    if uncovered:
        problems.append("not covered: " + ", ".join(uncovered))
    if ambiguous:
        problems.append("covered by conflicting tiles: " + ", ".join(ambiguous))
    if problems:
        raise HeatmapCoverageError(
            "heatmap cannot map every job (" + "; ".join(problems) + ")"
        )
    return temperatures


def build_temperature_profiles(
    jobs: Sequence[Job],
    heatmap_results_by_minute: Mapping[int, Mapping[str, Any]],
) -> dict[str, TemperatureProfile]:
    """Build chronologically ordered live profiles from sampled heatmaps.

    Keys are requested minutes of day (for example, ``8 * 60`` for 08:00).
    Every sample must cover every job. Profile certainty is the documented
    no-penalty sentinel, not a probability; calibration belongs in a later
    residual-based layer.
    """

    _validate_unique_job_ids(jobs)
    if not heatmap_results_by_minute:
        raise ValueError("heatmap_results_by_minute must contain at least one sample")

    samples: list[tuple[int, dict[str, float]]] = []
    for minute, result in heatmap_results_by_minute.items():
        if isinstance(minute, bool) or not isinstance(minute, int):
            raise TypeError("sample minute must be an integer minute of day")
        if not 0 <= minute < 24 * 60:
            raise ValueError("sample minute must be between 0 and 1439")
        try:
            temperatures = map_job_temperatures(jobs, result)
        except HeatmapCoverageError as exc:
            raise HeatmapCoverageError(f"sample at minute {minute}: {exc}") from exc
        samples.append((minute, temperatures))
    samples.sort(key=lambda item: item[0])

    return {
        job.job_id: TemperatureProfile(
            job_id=job.job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=minute,
                    temperature_c=temperatures[job.job_id],
                    certainty=UNCALIBRATED_CERTAINTY,
                )
                for minute, temperatures in samples
            ),
        )
        for job in jobs
    }


def _parse_geometry(
    geometry: Mapping[str, Any], *, path: str
) -> tuple[Literal["Polygon", "MultiPolygon"], ParsedPolygons]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return "Polygon", (_parse_polygon(coordinates, path=f"{path}.coordinates"),)
    if geometry_type == "MultiPolygon":
        if not _is_sequence(coordinates) or not coordinates:
            raise FortyGuardProtocolError(
                f"{path}.coordinates must contain at least one polygon"
            )
        return "MultiPolygon", tuple(
            _parse_polygon(value, path=f"{path}.coordinates[{index}]")
            for index, value in enumerate(coordinates)
        )
    raise FortyGuardProtocolError(f"{path}.type must be 'Polygon' or 'MultiPolygon'")


def _parse_polygon(value: Any, *, path: str) -> PolygonRings:
    if not _is_sequence(value) or not value:
        raise FortyGuardProtocolError(f"{path} must contain at least one linear ring")
    return tuple(
        _parse_ring(ring, path=f"{path}[{index}]") for index, ring in enumerate(value)
    )


def _parse_ring(value: Any, *, path: str) -> LinearRing:
    if not _is_sequence(value) or len(value) < 4:
        raise FortyGuardProtocolError(f"{path} must contain at least four positions")
    ring = tuple(
        _parse_position(position, path=f"{path}[{index}]")
        for index, position in enumerate(value)
    )
    if ring[0] != ring[-1]:
        raise FortyGuardProtocolError(f"{path} must be closed")
    if len(set(ring[:-1])) < 3:
        raise FortyGuardProtocolError(
            f"{path} must contain at least three distinct positions"
        )
    return ring


def _parse_position(value: Any, *, path: str) -> Position:
    if not _is_sequence(value) or len(value) < 2:
        raise FortyGuardProtocolError(f"{path} must contain longitude and latitude")
    longitude = _finite_number(value[0], path=f"{path}[0]")
    latitude = _finite_number(value[1], path=f"{path}[1]")
    if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
        raise FortyGuardProtocolError(f"{path} must contain WGS84 coordinates")
    return longitude, latitude


def _polygon_covers(point: Position, polygon: PolygonRings) -> bool:
    exterior_location = _ring_location(point, polygon[0])
    if exterior_location < 0:
        return False
    if exterior_location == 0:
        return True
    for hole in polygon[1:]:
        hole_location = _ring_location(point, hole)
        if hole_location == 0:
            return True
        if hole_location > 0:
            return False
    return True


def _ring_location(point: Position, ring: LinearRing) -> int:
    """Return -1 outside, 0 on the boundary, or 1 inside a linear ring."""

    x, y = point
    inside = False
    for start, end in zip(ring, ring[1:], strict=False):
        if _point_on_segment(point, start, end):
            return 0
        x0, y0 = start
        x1, y1 = end
        if (y0 > y) != (y1 > y):
            crossing_x = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < crossing_x:
                inside = not inside
    return 1 if inside else -1


def _point_on_segment(point: Position, start: Position, end: Position) -> bool:
    x, y = point
    x0, y0 = start
    x1, y1 = end
    cross_product = (x - x0) * (y1 - y0) - (y - y0) * (x1 - x0)
    return (
        cross_product == 0
        and min(x0, x1) <= x <= max(x0, x1)
        and min(y0, y1) <= y <= max(y0, y1)
    )


def _required_mapping(
    mapping: Mapping[str, Any], key: str, *, path: str
) -> Mapping[str, Any]:
    value = mapping.get(key)
    if not isinstance(value, Mapping):
        raise FortyGuardProtocolError(f"{path}.{key} must be an object")
    return value


def _finite_number(value: Any, *, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FortyGuardProtocolError(f"{path} must be a finite number")
    number = float(value)
    if not isfinite(number):
        raise FortyGuardProtocolError(f"{path} must be a finite number")
    return 0.0 if number == 0 else number


def _validate_unique_job_ids(jobs: Sequence[Job]) -> None:
    identifiers = [job.job_id for job in jobs]
    duplicates = sorted(
        identifier
        for identifier in set(identifiers)
        if identifiers.count(identifier) > 1
    )
    if duplicates:
        raise ValueError("job IDs must be unique: " + ", ".join(duplicates))


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )


__all__ = [
    "HeatmapCoverageError",
    "HeatmapTile",
    "UNCALIBRATED_CERTAINTY",
    "build_temperature_profiles",
    "extract_heatmap_tiles",
    "geometry_covers_point",
    "map_job_temperatures",
]
