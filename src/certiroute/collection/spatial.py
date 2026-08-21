"""Canonical GeoJSON tile geometry and stable spatial identifiers."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from typing import Any

from certiroute.collection._json import normalize_json_object


def canonical_tile_geometry(geometry: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize Polygon/MultiPolygon geometry for spatial matching.

    Ring rotation and winding direction do not change the result. Hole order and
    MultiPolygon member order are also normalized, while actual coordinates are
    never rounded.
    """

    source = normalize_json_object(geometry, path="$.geometry")
    geometry_type = source.get("type")
    coordinates = source.get("coordinates")
    if geometry_type == "Polygon":
        canonical_coordinates = _canonical_polygon(coordinates, "$.coordinates")
    elif geometry_type == "MultiPolygon":
        if not _is_sequence(coordinates) or not coordinates:
            raise ValueError("MultiPolygon coordinates must contain polygons")
        polygons = [
            _canonical_polygon(value, f"$.coordinates[{index}]")
            for index, value in enumerate(coordinates)
        ]
        canonical_coordinates = sorted(polygons, key=_canonical_json)
    else:
        raise ValueError("tile geometry type must be Polygon or MultiPolygon")
    return {"type": geometry_type, "coordinates": canonical_coordinates}


def tile_spatial_key(geometry: Mapping[str, Any]) -> str:
    """Return SHA-256 of canonical tile geometry."""

    canonical = _canonical_json(canonical_tile_geometry(geometry)).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _canonical_polygon(value: Any, path: str) -> list[list[list[float]]]:
    if not _is_sequence(value) or not value:
        raise ValueError(f"{path} must contain at least one linear ring")
    rings = [
        _canonical_ring(ring, f"{path}[{index}]") for index, ring in enumerate(value)
    ]
    return [rings[0], *sorted(rings[1:], key=_canonical_json)]


def _canonical_ring(value: Any, path: str) -> list[list[float]]:
    if not _is_sequence(value) or len(value) < 4:
        raise ValueError(f"{path} must contain at least four positions")
    positions = [
        _canonical_position(position, f"{path}[{index}]")
        for index, position in enumerate(value)
    ]
    if positions[0] != positions[-1]:
        raise ValueError(f"{path} must be closed")
    body = positions[:-1]
    if len(set(map(tuple, body))) < 3:
        raise ValueError(f"{path} must contain at least three distinct positions")

    orientations = (body, list(reversed(body)))
    candidates: list[list[list[float]]] = []
    for oriented in orientations:
        for index in range(len(oriented)):
            rotated = oriented[index:] + oriented[:index]
            candidates.append([*rotated, rotated[0]])
    return min(candidates, key=_canonical_json)


def _canonical_position(value: Any, path: str) -> list[float]:
    if not _is_sequence(value) or len(value) < 2:
        raise ValueError(f"{path} must contain longitude and latitude")
    position: list[float] = []
    for coordinate in value:
        if isinstance(coordinate, bool) or not isinstance(coordinate, (int, float)):
            raise TypeError(f"{path} coordinates must be numeric")
        number = float(coordinate)
        if not math.isfinite(number):
            raise ValueError(f"{path} coordinates must be finite")
        position.append(0.0 if number == 0 else number)
    if not -180 <= position[0] <= 180:
        raise ValueError(f"{path} longitude must be between -180 and 180")
    if not -90 <= position[1] <= 90:
        raise ValueError(f"{path} latitude must be between -90 and 90")
    return position


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    )
