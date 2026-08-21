"""Small GeoJSON helpers for FortyGuard area-of-interest requests."""

from collections.abc import Iterable

from certiroute.domain import GeoPoint
from certiroute.fortyguard.schemas import (
    PolygonFeature,
    PolygonFeatureCollection,
    PolygonGeometry,
)


def bounding_polygon(
    points: Iterable[GeoPoint], *, margin_degrees: float = 0.002
) -> PolygonFeatureCollection:
    """Build a closed rectangular AOI around points with a small margin.

    The caller remains responsible for keeping the polygon within FortyGuard's
    plan-specific maximum area.
    """

    point_list = list(points)
    if not point_list:
        raise ValueError("at least one point is required")
    if margin_degrees <= 0:
        raise ValueError("margin_degrees must be greater than zero")

    west = min(point.longitude for point in point_list) - margin_degrees
    east = max(point.longitude for point in point_list) + margin_degrees
    south = min(point.latitude for point in point_list) - margin_degrees
    north = max(point.latitude for point in point_list) + margin_degrees

    ring = [
        (west, south),
        (east, south),
        (east, north),
        (west, north),
        (west, south),
    ]
    return PolygonFeatureCollection(
        features=[PolygonFeature(geometry=PolygonGeometry(coordinates=[ring]))]
    )
