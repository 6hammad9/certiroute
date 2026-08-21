import json
from datetime import UTC, date, datetime, time, timedelta, timezone

import pytest

from certiroute.collection import (
    CacheCorruptionError,
    ForecastArchive,
    JsonDiskCache,
    UnsafeCachePayloadError,
    heatmap_request_fingerprint,
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
            start_time=time(14, 0),
        ),
        granularity=granularity,
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


def test_forecast_archive_records_normalized_utc_and_no_feature_secrets(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)
    request = _request(properties={"api_key": "must-not-be-written"})
    requested = datetime(2026, 8, 22, 10, tzinfo=timezone(timedelta(hours=2)))
    target = datetime(2026, 8, 22, 11, 30, tzinfo=timezone(timedelta(hours=2)))

    record = archive.record_forecast(
        request,
        requested_at_utc=requested,
        target_valid_at_utc=target,
        activity_id=" forecast-activity ",
        per_tile_forecasts=[
            {"tile_id": "tile-a", "forecast_temperature_c": 39.5},
            {"tile_id": "tile-b", "forecast_temperature_c": 40.0},
        ],
        raw_forecast_result={"stats_data": {"mean": 39.75}},
    )

    assert record.requested_at_utc == datetime(2026, 8, 22, 8, tzinfo=UTC)
    assert record.target_valid_at_utc == datetime(2026, 8, 22, 9, 30, tzinfo=UTC)
    assert record.lead_hours == 1.5
    assert record.activity_id == "forecast-activity"
    assert record.aoi["features"][0].keys() == {"type", "geometry"}
    assert archive.get_for_request(request) == record

    persisted = next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")
    assert "must-not-be-written" not in persisted
    assert "api_key" not in persisted
    assert '"requested_at_utc":"2026-08-22T08:00:00Z"' in persisted


def test_archive_attaches_vendor_relative_residuals_with_explicit_sign(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = archive.record_forecast(
        _request(),
        requested_at_utc=datetime(2026, 8, 22, 8, tzinfo=UTC),
        target_valid_at_utc=datetime(2026, 8, 22, 10, tzinfo=UTC),
        activity_id="forecast-1",
        per_tile_forecasts=[
            {"tile_id": "tile-a", "forecast_temperature_c": 40.0},
            {"tile_id": "tile-b", "forecast_temperature_c": 38.0},
        ],
    )

    updated = archive.record_vendor_relative_realization(
        forecast.request_fingerprint,
        recorded_at_utc=datetime(2026, 8, 22, 10, 5, tzinfo=UTC),
        activity_id="realization-1",
        per_tile_realizations=[
            {
                "tile_id": "tile-a",
                "vendor_relative_realization_temperature_c": 41.25,
            },
            {
                "tile_id": "tile-b",
                "vendor_relative_realization_temperature_c": 37.5,
            },
        ],
        raw_result={"source": "later-vendor-result"},
    )

    realization = updated.vendor_relative_realization
    assert realization is not None
    assert realization.residual_definition == (
        "vendor_relative_realization_minus_forecast"
    )
    assert [
        item.vendor_relative_residual_c for item in realization.per_tile_residuals
    ] == [
        1.25,
        -0.5,
    ]
    assert realization.mean_vendor_relative_residual_c == pytest.approx(0.375)
    assert archive.get(forecast.request_fingerprint) == updated

    persisted = next(tmp_path.rglob("*.json")).read_text(encoding="utf-8")
    assert "ground_truth" not in persisted
    assert "vendor_relative_residual_c" in persisted


def test_archive_rejects_unknown_tiles_and_accidental_realization_replacement(
    tmp_path,
) -> None:
    archive = ForecastArchive(tmp_path)
    forecast = archive.record_forecast(
        _request(),
        requested_at_utc=datetime(2026, 8, 22, 8, tzinfo=UTC),
        target_valid_at_utc=datetime(2026, 8, 22, 9, tzinfo=UTC),
        activity_id="forecast-1",
        per_tile_forecasts=[
            {"tile_id": "known", "forecast_temperature_c": 40.0},
        ],
    )
    common = {
        "recorded_at_utc": datetime(2026, 8, 22, 9, 5, tzinfo=UTC),
        "activity_id": "realization-1",
    }

    with pytest.raises(ValueError, match="do not match forecast tiles"):
        archive.record_vendor_relative_realization(
            forecast.request_fingerprint,
            **common,
            per_tile_realizations=[
                {
                    "tile_id": "unknown",
                    "vendor_relative_realization_temperature_c": 40.0,
                }
            ],
        )

    archive.record_vendor_relative_realization(
        forecast.request_fingerprint,
        **common,
        per_tile_realizations=[
            {
                "tile_id": "known",
                "vendor_relative_realization_temperature_c": 40.0,
            }
        ],
    )
    with pytest.raises(FileExistsError, match="already exists"):
        archive.record_vendor_relative_realization(
            forecast.request_fingerprint,
            **common,
            per_tile_realizations=[
                {
                    "tile_id": "known",
                    "vendor_relative_realization_temperature_c": 41.0,
                }
            ],
        )


def test_cache_rejects_secret_fields_and_path_traversal(tmp_path) -> None:
    cache = JsonDiskCache(tmp_path)
    fingerprint = "a" * 64

    with pytest.raises(UnsafeCachePayloadError, match="secret-like field"):
        cache.put(fingerprint, {"headers": {"api-key": "secret"}})
    with pytest.raises(ValueError, match="64 lowercase hex"):
        cache.get("../outside")
    assert list(tmp_path.rglob("*.json")) == []


def test_cache_atomic_failure_preserves_previous_entry(tmp_path, monkeypatch) -> None:
    cache = JsonDiskCache(tmp_path)
    fingerprint = "b" * 64
    cache.put(fingerprint, {"value": "original"})

    def fail_replace(source, destination) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("certiroute.collection.cache.os.replace", fail_replace)
    with pytest.raises(OSError, match="simulated"):
        cache.put(fingerprint, {"value": "replacement"})

    assert cache.get(fingerprint) == {"value": "original"}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_archive_requires_explicit_timezone_and_completed_payload(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)

    with pytest.raises(ValueError, match="timezone-aware"):
        archive.record_forecast(
            _request(),
            requested_at_utc=datetime(2026, 8, 22, 8),
            target_valid_at_utc=datetime(2026, 8, 22, 9, tzinfo=UTC),
            activity_id="forecast-1",
            raw_forecast_result={"complete": True},
        )

    with pytest.raises(ValueError, match="tile data or a raw result"):
        archive.record_forecast(
            _request(),
            requested_at_utc=datetime(2026, 8, 22, 8, tzinfo=UTC),
            target_valid_at_utc=datetime(2026, 8, 22, 9, tzinfo=UTC),
            activity_id="forecast-1",
        )


def test_cache_entry_is_valid_json_and_refuses_overwrite(tmp_path) -> None:
    fixed_now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    cache = JsonDiskCache(tmp_path, clock=lambda: fixed_now)
    fingerprint = "c" * 64
    path = cache.put(fingerprint, {"temperature_c": 39.5})

    entry = json.loads(path.read_text(encoding="utf-8"))
    assert entry["cache_schema_version"] == 1
    assert entry["request_fingerprint"] == fingerprint
    assert entry["stored_at_utc"] == "2026-08-21T12:00:00Z"
    with pytest.raises(FileExistsError):
        cache.put(fingerprint, {"temperature_c": 40.0}, overwrite=False)


def test_archive_detects_payload_fingerprint_mismatch(tmp_path) -> None:
    archive = ForecastArchive(tmp_path)
    record = archive.record_forecast(
        _request(),
        requested_at_utc=datetime(2026, 8, 22, 8, tzinfo=UTC),
        target_valid_at_utc=datetime(2026, 8, 22, 9, tzinfo=UTC),
        activity_id="forecast-1",
        raw_forecast_result={"complete": True},
    )
    path = next(tmp_path.rglob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["payload"]["request_fingerprint"] = "d" * 64
    path.write_text(json.dumps(entry), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="does not match"):
        archive.get(record.request_fingerprint)
