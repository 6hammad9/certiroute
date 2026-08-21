"""GeoJSON construction, measurement, and batching for FortyGuard AOIs."""

from collections.abc import Iterable
from dataclasses import dataclass
from math import cos, isfinite, radians

from certiroute.domain import GeoPoint
from certiroute.fortyguard.errors import FortyGuardAOITooLarge
from certiroute.fortyguard.schemas import (
    PolygonFeature,
    PolygonFeatureCollection,
    PolygonGeometry,
)

DEFAULT_MAX_AOI_AREA_SQUARE_MILES = 10.0
_EARTH_RADIUS_MILES = 3_958.7613


@dataclass(frozen=True)
class AoiCluster:
    """A compact group of points and the rectangular AOI that contains them."""

    points: tuple[GeoPoint, ...]
    polygon: PolygonFeatureCollection
    area_square_miles: float


def bounding_polygon(
    points: Iterable[GeoPoint], *, margin_degrees: float = 0.002
) -> PolygonFeatureCollection:
    """Build a closed rectangular AOI around points with a small margin."""

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


def polygon_area_square_miles(polygon_aoi: PolygonFeatureCollection) -> float:
    """Approximate a small GeoJSON AOI's surface area in square miles.

    Coordinates are projected onto a local tangent plane before applying the
    shoelace formula. This avoids treating longitude as a constant-distance
    axis and is accurate for the city-scale U.S. polygons accepted by the API.
    Interior rings are treated as holes, following GeoJSON ring ordering.
    """

    return sum(
        _polygon_geometry_area(feature.geometry) for feature in polygon_aoi.features
    )


def validate_aoi_area(
    polygon_aoi: PolygonFeatureCollection,
    *,
    max_area_square_miles: float = DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
) -> float:
    """Return the AOI area, raising before submission when it exceeds a plan."""

    _validate_area_limit(max_area_square_miles)
    area = polygon_area_square_miles(polygon_aoi)
    if area > max_area_square_miles:
        raise FortyGuardAOITooLarge(area, max_area_square_miles)
    return area


def cluster_points_into_aois(
    points: Iterable[GeoPoint],
    *,
    max_area_square_miles: float = DEFAULT_MAX_AOI_AREA_SQUARE_MILES,
    margin_degrees: float = 0.002,
) -> list[AoiCluster]:
    """Group points into deterministic, compact rectangular AOIs.

    The agglomerative pass repeatedly joins the pair with the smallest valid
    bounding AOI. It stops when no pair can be joined under the plan limit.
    Every input occurrence is retained, including duplicate coordinates.
    """

    _validate_area_limit(max_area_square_miles)
    if margin_degrees <= 0 or not isfinite(margin_degrees):
        raise ValueError("margin_degrees must be a finite value greater than zero")

    indexed_points = sorted(
        enumerate(points),
        key=lambda item: (
            item[1].longitude,
            item[1].latitude,
            item[0],
        ),
    )
    working_clusters: list[tuple[tuple[int, GeoPoint], ...]] = [
        (item,) for item in indexed_points
    ]

    # Fail clearly when even the required margin around one point cannot fit.
    for cluster in working_clusters:
        polygon = bounding_polygon(
            (item[1] for item in cluster), margin_degrees=margin_degrees
        )
        validate_aoi_area(polygon, max_area_square_miles=max_area_square_miles)

    while len(working_clusters) > 1:
        best_merge: (
            tuple[
                tuple[float, tuple[tuple[float, float, int], ...]],
                int,
                int,
                tuple[tuple[int, GeoPoint], ...],
            ]
            | None
        ) = None

        for first_index in range(len(working_clusters) - 1):
            for second_index in range(first_index + 1, len(working_clusters)):
                members = tuple(
                    sorted(
                        working_clusters[first_index] + working_clusters[second_index],
                        key=_indexed_point_key,
                    )
                )
                polygon = bounding_polygon(
                    (item[1] for item in members), margin_degrees=margin_degrees
                )
                area = polygon_area_square_miles(polygon)
                if area > max_area_square_miles:
                    continue

                signature = tuple(_indexed_point_key(item) for item in members)
                candidate = (
                    (area, signature),
                    first_index,
                    second_index,
                    members,
                )
                if best_merge is None or candidate[0] < best_merge[0]:
                    best_merge = candidate

        if best_merge is None:
            break

        _, first_index, second_index, members = best_merge
        working_clusters = [
            cluster
            for index, cluster in enumerate(working_clusters)
            if index not in (first_index, second_index)
        ]
        working_clusters.append(members)
        working_clusters.sort(key=_cluster_key)

    result: list[AoiCluster] = []
    for members in sorted(working_clusters, key=_cluster_key):
        cluster_points = tuple(item[1] for item in members)
        polygon = bounding_polygon(cluster_points, margin_degrees=margin_degrees)
        area = validate_aoi_area(polygon, max_area_square_miles=max_area_square_miles)
        result.append(
            AoiCluster(
                points=cluster_points,
                polygon=polygon,
                area_square_miles=area,
            )
        )
    return result


def _polygon_geometry_area(geometry: PolygonGeometry) -> float:
    outer_area = _ring_area_square_miles(geometry.coordinates[0])
    holes_area = sum(_ring_area_square_miles(ring) for ring in geometry.coordinates[1:])
    area = outer_area - holes_area
    if area < -1e-9:
        raise ValueError("polygon interior rings exceed its outer ring")
    return max(area, 0.0)


def _ring_area_square_miles(ring: list[tuple[float, float]]) -> float:
    longitudes: list[float] = []
    latitudes: list[float] = []

    previous_longitude: float | None = None
    for longitude, latitude in ring:
        if (
            not isfinite(longitude)
            or not isfinite(latitude)
            or not -180 <= longitude <= 180
            or not -90 <= latitude <= 90
        ):
            raise ValueError("polygon positions must be finite WGS84 coordinates")

        if previous_longitude is None:
            unwrapped_longitude = longitude
        else:
            delta = (longitude - previous_longitude + 180) % 360 - 180
            unwrapped_longitude = longitudes[-1] + delta
        longitudes.append(unwrapped_longitude)
        latitudes.append(latitude)
        previous_longitude = longitude

    unique_count = len(ring) - 1 if ring[0] == ring[-1] else len(ring)
    reference_longitude = longitudes[0]
    reference_latitude = sum(latitudes[:unique_count]) / unique_count
    longitude_scale = cos(radians(reference_latitude))
    positions = [
        (
            _EARTH_RADIUS_MILES
            * radians(longitude - reference_longitude)
            * longitude_scale,
            _EARTH_RADIUS_MILES * radians(latitude - reference_latitude),
        )
        for longitude, latitude in zip(longitudes, latitudes, strict=True)
    ]
    twice_area = sum(
        first_x * second_y - second_x * first_y
        for (first_x, first_y), (second_x, second_y) in zip(
            positions, positions[1:] + positions[:1], strict=True
        )
    )
    return abs(twice_area) / 2


def _validate_area_limit(max_area_square_miles: float) -> None:
    if not isfinite(max_area_square_miles) or max_area_square_miles <= 0:
        raise ValueError(
            "max_area_square_miles must be a finite value greater than zero"
        )


def _indexed_point_key(item: tuple[int, GeoPoint]) -> tuple[float, float, int]:
    index, point = item
    return (point.longitude, point.latitude, index)


def _cluster_key(
    cluster: tuple[tuple[int, GeoPoint], ...],
) -> tuple[tuple[float, float, int], ...]:
    return tuple(_indexed_point_key(item) for item in cluster)
