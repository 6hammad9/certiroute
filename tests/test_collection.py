import json
from datetime import UTC, date, datetime, time

import pytest

from certiroute.collection import (
    CacheCorruptionError,
    ForecastArchive,
    JsonDiskCache,
    UnsafeCachePayloadError,
    heatmap_request_fingerprint,
    tile_spatial_key,
)
from certiroute.fortyguard.schemas import (
    HeatmapRequest,
    PolygonFeature,
    PolygonFeatureCollection,
    PolygonGeometry,
    SingleHourDateTime,
)


def _request(
    *,
    granularity: int = 100,
    properties: dict[str, object] | None = None,
    target_time: time = time(14, 0),
) -> HeatmapRequest:
    return HeatmapRequest(
        polygon_aoi=PolygonFeatureCollection(
            features=[
                PolygonFeature(
                    properties=properties or {},
                    geometry=PolygonGeometry(
                        coordinates=[
                            [
                                (-112.1, 33.4),
                                (-112.0, 33.4),
                                (-112.0, 33.5),
                                (-112.1, 33.4),
                            ]
                        ]
                    ),
                )
            ]
        ),
        date_time=SingleHourDateTime(
            start_date=date(2026, 8, 22),
            start_time=target_time,
        ),
        granularity=granularity,
    )


def _tile_geometry(longitude: float) -> dict[str, object]:
    return {
        "type": "Polygon",
        "coordinates": [
            [
                [longitude, 33.4],
                [longitude + 0.01, 33.4],
                [longitude + 0.01, 33.41],
                [longitude, 33.41],
                [longitude, 33.4],
            ]
        ],
    }


def _equivalent_reversed_geometry(geometry: dict[str, object]) -> dict[str, object]:
    ring = geometry["coordinates"][0]
    body = ring[:-1]
    rotated = body[2:] + body[:2]
    reversed_ring = list(reversed(rotated))
    return {
        "coordinates": [[*reversed_ring, reversed_ring[0]]],
        "type": "Polygon",
    }


def _time_basis() -> dict[str, object]:
    return {
        "assumption": "Phoenix local civil time; fixed UTC-07 assumed",
        "utc_offset_minutes": -7 * 60,
    }


def _record_two_tile_forecast(
    archive: ForecastArchive,
    *,
    activity_id: str = "forecast-1",
    requested_at_utc: datetime = datetime(2026, 8, 22, 20, tzinfo=UTC),
):
    return archive.record_forecast(
        _request(),
        requested_at_utc=requested_at_utc,
        request_time_basis=_time_basis(),
        activity_id=activity_id,
        per_tile_forecasts=[
            {
                "geometry": _tile_geometry(-112.10),
                "forecast_temperature_c": 40.0,
                "vendor_tile_id": "forecast-vendor-a",
            },
            {
                "geometry": _tile_geometry(-112.08),
                "forecast_temperature_c": 38.0,
                "vendor_tile_id": "forecast-vendor-b",
            },
        ],
    )


def test_request_fingerprint_is_stable_across_mapping_key_order() -> None:
    request = _request(properties={"zone": "north", "crew": 4})
    payload = request.model_dump(mode="json")
    reordered = dict(reversed(list(payload.items())))
    reordered["polygon_aoi"]["features"][0]["properties"] = {
        "crew": 4,
        "zone": "north",
    }

    assert heatmap_request_fingerprint(request) == heatmap_request_fingerprint(
        reordered
    )
    assert heatmap_request_fingerprint(request) != heatmap_request_fingerprint(
        _request(granularity=80)
    )


def test_spatial_key_uses_canonical_geometry_not_vendor_tile_id() -> None:
    geometry = _tile_geometry(-112.10)
    equivalent = _equivalent_reversed_geometry(geometry)

    assert tile_spatial_key(geometry) == tile_spatial_key(equivalent)
    assert tile_spatial_key(geometry) != tile_spatial_key(_tile_geometry(-112.08))


def test_same_request_has_distinct_append_only_vintages_and_explicit_latest(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)
    first = _record_two_tile_forecast(archive, activity_id="forecast-1")
    second = _record_two_tile_forecast(
        archive,
        activity_id="forecast-2",
        requested_at_utc=datetime(2026, 8, 22, 20, 5, tzinfo=UTC),
    )

    assert first.request_fingerprint == second.request_fingerprint
    assert first.record_id != second.record_id
    assert archive.get_forecast(first.record_id) == first
    assert archive.list_forecast_vintages(_request()) == (first, second)
    assert archive.latest_forecast_for_request(_request()) == second
    assert len(list((tmp_path / "forecasts").rglob("*.json"))) == 2

    with pytest.raises(FileExistsError, match="record already exists"):
        _record_two_tile_forecast(archive, activity_id="forecast-1")
    assert archive.get_forecast(first.record_id) == first


def test_forecast_persists_request_wall_clock_and_explicit_time_assumption(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)
    request = _request(properties={"api_key": "must-not-be-written"})
    record = archive.record_forecast(
        request,
        requested_at_utc=datetime(2026, 8, 22, 20, tzinfo=UTC),
        request_time_basis=_time_basis(),
        activity_id="forecast-1",
        raw_forecast_result={"stats_data": {"mean": 39.75}},
    )

    assert record.request_start_date == date(2026, 8, 22)
    assert record.request_start_time == time(14, 0)
    assert record.request_time_basis.source == "caller_supplied_assumption"
    assert record.request_time_basis.utc_offset_minutes == -420
    assert record.assumed_target_valid_at_utc == datetime(2026, 8, 22, 21, tzinfo=UTC)
    assert record.assumed_lead_hours == 1.0

    persisted = next((tmp_path / "forecasts").rglob("*.json")).read_text(
        encoding="utf-8"
    )
    assert "must-not-be-written" not in persisted
    assert "api_key" not in persisted
    assert '"request_start_date":"2026-08-22"' in persisted
    assert '"request_start_time":"14:00"' in persisted
    assert "caller_supplied_assumption" in persisted


def test_realization_matches_canonical_geometry_with_exact_coverage(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = _record_two_tile_forecast(archive)

    realization = archive.record_vendor_relative_realization(
        forecast.record_id,
        request=_request(),
        request_time_basis=_time_basis(),
        recorded_at_utc=datetime(2026, 8, 22, 21, 5, tzinfo=UTC),
        activity_id="realization-1",
        per_tile_realizations=[
            {
                "geometry": _equivalent_reversed_geometry(_tile_geometry(-112.08)),
                "vendor_relative_realization_temperature_c": 37.5,
                "vendor_tile_id": "different-realization-b",
            },
            {
                "geometry": _equivalent_reversed_geometry(_tile_geometry(-112.10)),
                "vendor_relative_realization_temperature_c": 41.25,
                "vendor_tile_id": "different-realization-a",
            },
        ],
    )

    assert [
        item.vendor_relative_residual_c for item in realization.per_tile_residuals
    ] == [1.25, -0.5]
    assert realization.mean_vendor_relative_residual_c == pytest.approx(0.375)
    assert realization.residual_definition == (
        "vendor_relative_realization_minus_forecast"
    )
    assert archive.get_vendor_relative_realization(realization.record_id) == realization


def test_realization_rejects_missing_or_unexpected_geometry(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = _record_two_tile_forecast(archive)

    with pytest.raises(ValueError, match="exactly cover") as exc_info:
        archive.record_vendor_relative_realization(
            forecast.record_id,
            request=_request(),
            request_time_basis=_time_basis(),
            recorded_at_utc=datetime(2026, 8, 22, 21, 5, tzinfo=UTC),
            activity_id="realization-1",
            per_tile_realizations=[
                {
                    "geometry": _tile_geometry(-112.10),
                    "vendor_relative_realization_temperature_c": 41.0,
                },
                {
                    "geometry": _tile_geometry(-112.06),
                    "vendor_relative_realization_temperature_c": 39.0,
                },
            ],
        )

    assert "missing=" in str(exc_info.value)
    assert "unexpected=" in str(exc_info.value)
    assert list((tmp_path / "vendor_relative_realizations").rglob("*.json")) == []


def test_realization_rejects_wrong_hour_and_time_assumption(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = _record_two_tile_forecast(archive)
    values = [
        {
            "geometry": _tile_geometry(-112.10),
            "vendor_relative_realization_temperature_c": 41.0,
        },
        {
            "geometry": _tile_geometry(-112.08),
            "vendor_relative_realization_temperature_c": 39.0,
        },
    ]
    common = {
        "recorded_at_utc": datetime(2026, 8, 22, 22, 5, tzinfo=UTC),
        "activity_id": "realization-1",
        "per_tile_realizations": values,
    }

    with pytest.raises(ValueError, match="must match the selected forecast"):
        archive.record_vendor_relative_realization(
            forecast.record_id,
            request=_request(target_time=time(15, 0)),
            request_time_basis=_time_basis(),
            **common,
        )
    with pytest.raises(ValueError, match="time assumption must match"):
        archive.record_vendor_relative_realization(
            forecast.record_id,
            request=_request(),
            request_time_basis={
                "assumption": "Different caller assumption",
                "utc_offset_minutes": -6 * 60,
            },
            **common,
        )

    assert list((tmp_path / "vendor_relative_realizations").rglob("*.json")) == []


def test_realizations_are_separate_append_only_vintages(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = _record_two_tile_forecast(archive)
    forecast_path = next((tmp_path / "forecasts").rglob("*.json"))
    original_forecast_bytes = forecast_path.read_bytes()
    values = [
        {
            "geometry": _tile_geometry(-112.10),
            "vendor_relative_realization_temperature_c": 41.0,
        },
        {
            "geometry": _tile_geometry(-112.08),
            "vendor_relative_realization_temperature_c": 39.0,
        },
    ]
    first = archive.record_vendor_relative_realization(
        forecast.record_id,
        request=_request(),
        request_time_basis=_time_basis(),
        recorded_at_utc=datetime(2026, 8, 22, 21, 5, tzinfo=UTC),
        activity_id="realization-1",
        per_tile_realizations=values,
    )
    second = archive.record_vendor_relative_realization(
        forecast.record_id,
        request=_request(),
        request_time_basis=_time_basis(),
        recorded_at_utc=datetime(2026, 8, 22, 21, 10, tzinfo=UTC),
        activity_id="realization-2",
        per_tile_realizations=values,
    )

    assert first.record_id != second.record_id
    assert archive.list_vendor_relative_realizations(forecast.record_id) == (
        first,
        second,
    )
    assert archive.latest_vendor_relative_realization(forecast.record_id) == second
    assert forecast_path.read_bytes() == original_forecast_bytes

    with pytest.raises(FileExistsError, match="record already exists"):
        archive.record_vendor_relative_realization(
            forecast.record_id,
            request=_request(),
            request_time_basis=_time_basis(),
            recorded_at_utc=datetime(2026, 8, 22, 21, 5, tzinfo=UTC),
            activity_id="realization-1",
            per_tile_realizations=values,
        )


def test_cache_rejects_secret_fields_and_path_traversal(tmp_path) -> None:
    cache = JsonDiskCache(tmp_path)
    identifier = "a" * 64

    with pytest.raises(UnsafeCachePayloadError, match="secret-like field"):
        cache.add(identifier, {"headers": {"api-key": "secret"}})
    with pytest.raises(ValueError, match="64 lowercase hex"):
        cache.get("../outside")
    assert list(tmp_path.rglob("*.json")) == []


def test_cache_atomic_publication_failure_preserves_existing_record(
    tmp_path, monkeypatch
) -> None:
    cache = JsonDiskCache(tmp_path)
    identifier = "b" * 64
    cache.add(identifier, {"value": "original"})

    def fail_link(source, destination) -> None:
        raise OSError("simulated publication failure")

    monkeypatch.setattr("certiroute.collection.cache.os.link", fail_link)
    with pytest.raises(OSError, match="simulated"):
        cache.add(identifier, {"value": "replacement"})

    assert cache.get(identifier) == {"value": "original"}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_archive_requires_aware_issuance_and_nonnegative_assumed_lead(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        archive.record_forecast(
            _request(),
            requested_at_utc=datetime(2026, 8, 22, 20),
            request_time_basis=_time_basis(),
            activity_id="forecast-1",
            raw_forecast_result={"complete": True},
        )
    with pytest.raises(ValueError, match="cannot precede"):
        archive.record_forecast(
            _request(),
            requested_at_utc=datetime(2026, 8, 22, 22, tzinfo=UTC),
            request_time_basis=_time_basis(),
            activity_id="forecast-2",
            raw_forecast_result={"complete": True},
        )


def test_cache_entry_is_valid_json_and_append_only(tmp_path) -> None:
    fixed_now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    cache = JsonDiskCache(tmp_path, clock=lambda: fixed_now)
    identifier = "c" * 64
    path = cache.add(identifier, {"temperature_c": 39.5})

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["cache_schema_version"] == 3
    assert entry["record_id"] == identifier
    assert entry["stored_at_utc"] == "2026-08-21T12:00:00Z"
    assert len(entry["payload_sha256"]) == 64
    with pytest.raises(FileExistsError):
        cache.add(identifier, {"temperature_c": 40.0})


def test_archive_detects_syntactically_valid_payload_corruption(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    record = archive.record_forecast(
        _request(),
        requested_at_utc=datetime(2026, 8, 22, 20, tzinfo=UTC),
        request_time_basis=_time_basis(),
        activity_id="forecast-1",
        raw_forecast_result={"complete": True},
    )
    path = next((tmp_path / "forecasts").rglob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["payload"]["raw_forecast_result"]["complete"] = False
    path.write_text(json.dumps(entry), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="checksum"):
        archive.get_forecast(record.record_id)
