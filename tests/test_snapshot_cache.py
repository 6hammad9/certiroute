import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from certiroute.collection import (
    CacheCorruptionError,
    HeatmapSnapshotStore,
    SnapshotTemporalScope,
    UnsafeCachePayloadError,
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


def _publish(
    store: HeatmapSnapshotStore,
    *,
    activity_id: str = "activity-1",
    collected_at_utc: datetime = datetime(2026, 8, 22, 12, tzinfo=UTC),
    temporal_scope: SnapshotTemporalScope = SnapshotTemporalScope.HISTORICAL,
):
    return store.publish(
        _request(),
        activity_id=activity_id,
        collected_at_utc=collected_at_utc,
        temporal_scope=temporal_scope,
        raw_result={"stats_data": {"temperature_stats": {"mean": 39.5}}},
    )


def test_snapshot_persists_contract_result_metadata_and_integrity(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    snapshot = _publish(store)

    assert (
        snapshot.request_fingerprint
        == store.list_for_request(_request())[0].request_fingerprint
    )
    assert snapshot.activity_id == "activity-1"
    assert snapshot.collected_at_utc == datetime(2026, 8, 22, 12, tzinfo=UTC)
    assert snapshot.request_contract["granularity"] == 100
    assert snapshot.raw_result["stats_data"]["temperature_stats"]["mean"] == 39.5
    assert len(snapshot.content_checksum_sha256) == 64
    assert store.get(snapshot.snapshot_id) == snapshot

    entry = json.loads(next(tmp_path.rglob("*.json")).read_text(encoding="utf-8"))
    assert entry["payload"]["completion_status"] == "completed"
    assert entry["payload"]["request_contract"]["date_time"] == {
        "filter_type": 1,
        "start_date": "2026-08-22",
        "start_time": "14:00",
    }


def test_default_storage_root_is_under_git_ignored_data_raw() -> None:
    store = HeatmapSnapshotStore()

    expected_suffix = Path("data/raw/fortyguard_heatmap_snapshots")
    assert str(store.root).replace("\\", "/").endswith(expected_suffix.as_posix())


def test_historical_exact_request_reuses_without_ttl(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    older = _publish(store, activity_id="old")
    newer = _publish(
        store,
        activity_id="new",
        collected_at_utc=datetime(2026, 8, 23, 12, tzinfo=UTC),
    )

    assert store.lookup_historical(_request()) == newer
    assert store.lookup_historical(_request(granularity=80)) is None
    assert store.list_for_request(_request()) == (older, newer)


def test_current_or_forecast_reuse_requires_positive_ttl_and_fresh_entry(
    tmp_path,
) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    snapshot = _publish(
        store,
        temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST,
    )

    with pytest.raises(ValueError, match="explicit positive"):
        store.lookup_current_or_forecast(_request(), ttl=timedelta(0))
    assert (
        store.lookup_current_or_forecast(
            _request(),
            ttl=timedelta(minutes=30),
            now_utc=datetime(2026, 8, 22, 12, 20, tzinfo=UTC),
        )
        == snapshot
    )
    assert (
        store.lookup_current_or_forecast(
            _request(),
            ttl=timedelta(minutes=30),
            now_utc=datetime(2026, 8, 22, 12, 31, tzinfo=UTC),
        )
        is None
    )


def test_lookup_never_crosses_temporal_reuse_scopes(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    historical = _publish(store)

    assert (
        store.lookup_current_or_forecast(
            _request(),
            ttl=timedelta(days=365),
            now_utc=historical.collected_at_utc + timedelta(days=1),
        )
        is None
    )


def test_snapshot_identity_is_append_only_and_multiple_vintages_are_listed(
    tmp_path,
) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    first = _publish(store)
    second = _publish(
        store,
        activity_id="activity-2",
        collected_at_utc=datetime(2026, 8, 22, 12, 5, tzinfo=UTC),
    )

    assert first.snapshot_id != second.snapshot_id
    assert store.list_for_request(_request()) == (first, second)
    with pytest.raises(FileExistsError, match="record already exists"):
        _publish(store)
    assert store.get(first.snapshot_id) == first


def test_checksum_detects_tampered_completed_result(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)
    snapshot = _publish(store)
    path = next(tmp_path.rglob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    entry["payload"]["raw_result"]["stats_data"]["temperature_stats"]["mean"] = 99.0
    path.write_text(json.dumps(entry), encoding="utf-8")

    with pytest.raises(CacheCorruptionError, match="checksum"):
        store.get(snapshot.snapshot_id)


def test_snapshot_store_rejects_secret_fields(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)

    with pytest.raises(UnsafeCachePayloadError, match="secret-like field"):
        store.publish(
            _request(properties={"api-key": "do-not-write"}),
            activity_id="activity-1",
            collected_at_utc=datetime(2026, 8, 22, 12, tzinfo=UTC),
            temporal_scope=SnapshotTemporalScope.HISTORICAL,
            raw_result={"complete": True},
        )
    with pytest.raises(UnsafeCachePayloadError, match="secret-like field"):
        store.publish(
            _request(),
            activity_id="activity-1",
            collected_at_utc=datetime(2026, 8, 22, 12, tzinfo=UTC),
            temporal_scope=SnapshotTemporalScope.HISTORICAL,
            raw_result={"authorization": "Bearer do-not-write"},
        )
    assert list(tmp_path.rglob("*.json")) == []


def test_concurrent_same_identity_publishes_exactly_once(tmp_path) -> None:
    store = HeatmapSnapshotStore(tmp_path)

    def attempt_publish():
        try:
            return _publish(store)
        except FileExistsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _: attempt_publish(), range(2)))

    published = [outcome for outcome in outcomes if outcome is not None]
    assert len(published) == 1
    assert len(list(tmp_path.rglob("*.json"))) == 1
    assert store.get(published[0].snapshot_id) == published[0]
