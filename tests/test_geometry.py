import pytest

from certiroute.domain import GeoPoint
from certiroute.fortyguard.errors import FortyGuardAOITooLarge
from certiroute.fortyguard.geometry import (
    bounding_polygon,
    cluster_points_into_aois,
    polygon_area_square_miles,
    validate_aoi_area,
)
from certiroute.fortyguard.schemas import PolygonGeometry


def test_bounding_polygon_is_closed_and_uses_geojson_order() -> None:
    polygon = bounding_polygon(
        [GeoPoint(latitude=33.45, longitude=-112.07)], margin_degrees=0.01
    )

    ring = polygon.features[0].geometry.coordinates[0]

    assert ring[0] == ring[-1]
    assert ring[0] == pytest.approx((-112.08, 33.44))
    assert ring[2] == pytest.approx((-112.06, 33.46))


def test_bounding_polygon_rejects_empty_points() -> None:
    with pytest.raises(ValueError, match="at least one point is required"):
        bounding_polygon([])


def test_polygon_area_accounts_for_latitude_at_city_scale() -> None:
    polygon = bounding_polygon(
        [GeoPoint(latitude=33.45, longitude=-112.07)], margin_degrees=0.005
    )

    # At Phoenix's latitude, a 0.01° square is about 0.40 mi².
    assert polygon_area_square_miles(polygon) == pytest.approx(0.397, rel=0.01)


def test_validate_aoi_area_rejects_an_aoi_over_the_plan_limit() -> None:
    polygon = bounding_polygon(
        [
            GeoPoint(latitude=33.45, longitude=-112.07),
            GeoPoint(latitude=33.50, longitude=-112.00),
        ]
    )

    with pytest.raises(FortyGuardAOITooLarge) as error:
        validate_aoi_area(polygon, max_area_square_miles=10)

    assert error.value.area_square_miles > 10
    assert error.value.limit_square_miles == 10


def test_cluster_points_builds_deterministic_compact_aois_without_drops() -> None:
    points = [
        GeoPoint(latitude=33.45, longitude=-112.07),
        GeoPoint(latitude=33.45, longitude=-111.90),
        GeoPoint(latitude=33.45, longitude=-112.06),
        GeoPoint(latitude=33.45, longitude=-111.89),
    ]

    clusters = cluster_points_into_aois(points, max_area_square_miles=0.5)
    reversed_clusters = cluster_points_into_aois(
        reversed(points), max_area_square_miles=0.5
    )

    coordinates = [
        [point.geojson_position for point in cluster.points] for cluster in clusters
    ]
    reversed_coordinates = [
        [point.geojson_position for point in cluster.points]
        for cluster in reversed_clusters
    ]
    assert coordinates == reversed_coordinates
    assert coordinates == [
        [(-112.07, 33.45), (-112.06, 33.45)],
        [(-111.9, 33.45), (-111.89, 33.45)],
    ]
    assert sum(len(cluster.points) for cluster in clusters) == len(points)
    assert all(cluster.area_square_miles <= 0.5 for cluster in clusters)


def test_cluster_points_rejects_limit_too_small_for_one_point() -> None:
    point = GeoPoint(latitude=33.45, longitude=-112.07)

    with pytest.raises(FortyGuardAOITooLarge):
        cluster_points_into_aois([point], max_area_square_miles=0.01)


def test_self_intersecting_polygon_cannot_bypass_area_preflight() -> None:
    bow_tie = [
        (-112.5, 33.0),
        (-111.5, 34.0),
        (-112.5, 34.0),
        (-111.5, 33.0),
        (-112.5, 33.0),
    ]

    with pytest.raises(ValueError, match="self-intersect"):
        PolygonGeometry(coordinates=[bow_tie])
