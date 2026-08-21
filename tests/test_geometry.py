import pytest

from certiroute.domain import GeoPoint
from certiroute.fortyguard.geometry import bounding_polygon


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
