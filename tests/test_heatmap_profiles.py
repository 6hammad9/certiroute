from __future__ import annotations

import math

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard.errors import FortyGuardProtocolError
from certiroute.fortyguard.heatmap_profiles import (
    UNCALIBRATED_CERTAINTY,
    HeatmapCoverageError,
    build_temperature_profiles,
    extract_heatmap_tiles,
    geometry_covers_point,
    map_job_temperatures,
)


def _ring(
    minimum_x: float, minimum_y: float, maximum_x: float, maximum_y: float
) -> list[list[float]]:
    return [
        [minimum_x, minimum_y],
        [maximum_x, minimum_y],
        [maximum_x, maximum_y],
        [minimum_x, maximum_y],
        [minimum_x, minimum_y],
    ]


def _feature(
    geometry: dict,
    temperature: object,
    *,
    tile_id: str | None = None,
) -> dict:
    properties = {"average_temperature": temperature}
    if tile_id is not None:
        properties["tile_id"] = tile_id
    return {
        "type": "Feature",
        "geometry": geometry,
        "properties": properties,
    }


def _result(*features: dict) -> dict:
    return {
        "map_data": {
            "type": "FeatureCollection",
            "features": list(features),
        }
    }


def _polygon(*rings: list[list[float]]) -> dict:
    return {"type": "Polygon", "coordinates": list(rings)}


def _job(job_id: str, longitude: float, latitude: float) -> Job:
    return Job(
        job_id=job_id,
        name=job_id,
        location=GeoPoint(latitude=latitude, longitude=longitude),
        duration_minutes=30,
    )


def test_extracts_polygon_and_multipolygon_tiles() -> None:
    result = _result(
        _feature(_polygon(_ring(0, 0, 1, 1)), 31, tile_id="polygon"),
        _feature(
            {
                "type": "MultiPolygon",
                "coordinates": [
                    [_ring(2, 0, 3, 1)],
                    [_ring(4, 0, 5, 1)],
                ],
            },
            37.25,
            tile_id="multi",
        ),
    )

    tiles = extract_heatmap_tiles(result)

    assert [(tile.geometry_type, tile.average_temperature_c) for tile in tiles] == [
        ("Polygon", 31.0),
        ("MultiPolygon", 37.25),
    ]
    assert tiles[1].covers(GeoPoint(longitude=4.5, latitude=0.5))


def test_polygon_coverage_honors_holes_and_all_boundaries() -> None:
    geometry = _polygon(_ring(0, 0, 10, 10), _ring(4, 4, 6, 6))

    assert geometry_covers_point(geometry, GeoPoint(longitude=2, latitude=2))
    assert not geometry_covers_point(geometry, GeoPoint(longitude=5, latitude=5))
    assert geometry_covers_point(geometry, GeoPoint(longitude=0, latitude=5))
    assert geometry_covers_point(geometry, GeoPoint(longitude=4, latitude=5))
    assert not geometry_covers_point(geometry, GeoPoint(longitude=11, latitude=5))


@pytest.mark.parametrize("temperature", [math.nan, math.inf, -math.inf, True, "32"])
def test_rejects_non_finite_or_non_numeric_average_temperature(
    temperature: object,
) -> None:
    with pytest.raises(FortyGuardProtocolError, match="finite number"):
        extract_heatmap_tiles(
            _result(_feature(_polygon(_ring(0, 0, 1, 1)), temperature))
        )


def test_rejects_missing_or_malformed_feature_data() -> None:
    with pytest.raises(FortyGuardProtocolError, match="features"):
        extract_heatmap_tiles({"map_data": {"type": "FeatureCollection"}})
    with pytest.raises(FortyGuardProtocolError, match="must be closed"):
        extract_heatmap_tiles(
            _result(
                _feature(
                    _polygon([[0, 0], [1, 0], [1, 1], [0, 1]]),
                    30,
                )
            )
        )


def test_maps_every_job_by_geometry_not_feature_order() -> None:
    jobs = [_job("WEST", 0.5, 0.5), _job("EAST", 1.5, 0.5)]
    result = _result(
        _feature(_polygon(_ring(1, 0, 2, 1)), 40),
        _feature(_polygon(_ring(0, 0, 1, 1)), 30),
    )

    assert map_job_temperatures(jobs, result) == {"WEST": 30, "EAST": 40}


def test_uncovered_and_conflicting_boundary_jobs_raise_clear_errors() -> None:
    result = _result(
        _feature(_polygon(_ring(0, 0, 1, 1)), 30),
        _feature(_polygon(_ring(1, 0, 2, 1)), 40),
    )

    with pytest.raises(HeatmapCoverageError, match="not covered: OUTSIDE"):
        map_job_temperatures([_job("OUTSIDE", 3, 0.5)], result)
    with pytest.raises(HeatmapCoverageError, match="conflicting tiles: EDGE"):
        map_job_temperatures([_job("EDGE", 1, 0.5)], result)


def test_identical_temperature_boundary_overlap_is_unambiguous() -> None:
    result = _result(
        _feature(_polygon(_ring(0, 0, 1, 1)), 30),
        _feature(_polygon(_ring(1, 0, 2, 1)), 30),
    )

    assert map_job_temperatures([_job("EDGE", 1, 0.5)], result) == {"EDGE": 30}


def test_builds_ordered_profiles_with_neutral_uncalibrated_sentinel() -> None:
    jobs = [_job("WEST", 0.5, 0.5), _job("EAST", 1.5, 0.5)]

    def sampled(west: float, east: float) -> dict:
        return _result(
            _feature(_polygon(_ring(0, 0, 1, 1)), west),
            _feature(_polygon(_ring(1, 0, 2, 1)), east),
        )

    profiles = build_temperature_profiles(
        jobs,
        {
            10 * 60: sampled(32, 35),
            8 * 60: sampled(25, 27),
            9 * 60: sampled(29, 31),
        },
    )

    assert list(profiles) == ["WEST", "EAST"]
    assert [point.minute_of_day for point in profiles["WEST"].points] == [
        8 * 60,
        9 * 60,
        10 * 60,
    ]
    assert [point.temperature_c for point in profiles["WEST"].points] == [25, 29, 32]
    assert {
        point.certainty for profile in profiles.values() for point in profile.points
    } == {UNCALIBRATED_CERTAINTY}


def test_profile_builder_identifies_the_failing_sample_minute() -> None:
    job = _job("JOB", 0.5, 0.5)
    result = _result(_feature(_polygon(_ring(2, 2, 3, 3)), 30))

    with pytest.raises(HeatmapCoverageError, match="sample at minute 480"):
        build_temperature_profiles([job], {480: result})


def test_profile_builder_rejects_duplicate_job_ids_and_invalid_minutes() -> None:
    job = _job("JOB", 0.5, 0.5)
    result = _result(_feature(_polygon(_ring(0, 0, 1, 1)), 30))

    with pytest.raises(ValueError, match="job IDs must be unique"):
        build_temperature_profiles([job, job], {480: result})
    with pytest.raises(ValueError, match="between 0 and 1439"):
        build_temperature_profiles([job], {1440: result})
