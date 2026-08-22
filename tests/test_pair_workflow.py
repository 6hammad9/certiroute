import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta

import pytest
from pydantic import ValidationError

from certiroute.collection import (
    HeatmapSnapshotStore,
    RequestTimeBasis,
    SnapshotTemporalScope,
)
from certiroute.collection.pair_workflow import (
    ForecastAction,
    ForecastPairManifest,
    ForecastPairRepository,
    ForecastSemanticsUnverifiedError,
    apply_forecast_plan,
    apply_realization_plan,
    build_archive_status,
    build_vendor_relative_report,
    load_manifest,
    plan_forecast_collection,
    plan_realization_collection,
)
from certiroute.fortyguard.schemas import (
    HeatmapRequest,
    PolygonFeature,
    PolygonFeatureCollection,
    PolygonGeometry,
    SingleHourDateTime,
)

NOW = datetime(2026, 8, 22, 12, tzinfo=UTC)
BASIS = RequestTimeBasis(
    assumption="Test-only UTC wall-clock assumption; not vendor-confirmed.",
    utc_offset_minutes=0,
)


def _request(
    *,
    target_hour: int = 15,
    granularity: int = 100,
    west: float = -112.01,
) -> HeatmapRequest:
    return HeatmapRequest(
        polygon_aoi=PolygonFeatureCollection(
            features=[
                PolygonFeature(
                    geometry=PolygonGeometry(
                        coordinates=[
                            [
                                (west, 33.44),
                                (west + 0.01, 33.44),
                                (west + 0.01, 33.45),
                                (west, 33.44),
                            ]
                        ]
                    )
                )
            ]
        ),
        date_time=SingleHourDateTime(
            start_date=date(2026, 8, 22),
            start_time=time(target_hour),
        ),
        granularity=granularity,
    )


def _result(temperature: float = 40.0) -> dict[str, object]:
    return {
        "map_data": {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [-112.01, 33.44],
                                [-112.00, 33.44],
                                [-112.00, 33.45],
                                [-112.01, 33.44],
                            ]
                        ],
                    },
                    "properties": {
                        "average_temperature": temperature,
                        "tile_id": "vendor-tile-1",
                    },
                }
            ],
        }
    }


def _manifest(request: HeatmapRequest | None = None) -> ForecastPairManifest:
    return ForecastPairManifest(
        request_time_basis=BASIS,
        requests=(request or _request(),),
    )


def _record_forecast(
    repository: ForecastPairRepository,
    *,
    request: HeatmapRequest | None = None,
    requested_at: datetime = NOW,
    activity_id: str = "forecast-1",
    temperature: float = 40.0,
    basis: RequestTimeBasis = BASIS,
):
    selected = request or _request()
    geometry = _result(temperature)["map_data"]["features"][0]["geometry"]
    return repository.archive.record_forecast(
        selected,
        requested_at_utc=requested_at,
        request_time_basis=basis,
        activity_id=activity_id,
        per_tile_forecasts=[
            {
                "geometry": geometry,
                "forecast_temperature_c": temperature,
                "vendor_tile_id": "vendor-tile-1",
            }
        ],
    )


class CountingClient:
    def __init__(self) -> None:
        self.calls = 0

    def create_heatmap(self, request, **kwargs):
        self.calls += 1
        return "new-activity", _result()


class NoLookupBeforeMaturityStore:
    def __init__(self) -> None:
        self.validation_token = object()
        self.calls = 0

    def list_for_requests(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("snapshot lookup occurred before target maturity")


def test_manifest_requires_explicit_time_basis_and_unique_requests() -> None:
    with pytest.raises(ValidationError, match="request_time_basis"):
        ForecastPairManifest.model_validate({"requests": [_request()]})

    with pytest.raises(ValidationError, match="must be unique"):
        ForecastPairManifest(
            request_time_basis=BASIS, requests=(_request(), _request())
        )


def test_manifest_loader_rejects_secret_like_fields(tmp_path) -> None:
    payload = _manifest().model_dump(mode="json")
    payload["requests"][0]["polygon_aoi"]["features"][0]["properties"] = {
        "api_key": "must-not-enter-a-request-manifest"
    }
    path = tmp_path / "unsafe.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="secret-like field"):
        load_manifest(path)


def test_forecast_plan_rejects_past_and_beyond_documented_horizon(tmp_path) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    store = HeatmapSnapshotStore(tmp_path / "snapshots")

    with pytest.raises(ValueError, match="strictly after"):
        plan_forecast_collection(
            _manifest(_request(target_hour=12)),
            repository,
            store,
            now_utc=NOW,
        )
    with pytest.raises(ValueError, match="12-hour horizon"):
        plan_forecast_collection(
            _manifest(
                HeatmapRequest(
                    polygon_aoi=_request().polygon_aoi,
                    date_time=SingleHourDateTime(
                        start_date=date(2026, 8, 23),
                        start_time=time(1),
                    ),
                    granularity=100,
                )
            ),
            repository,
            store,
            now_utc=NOW,
        )


def test_forecast_dry_plan_and_cap_fail_before_client_or_archive(tmp_path) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    client = CountingClient()
    plan = plan_forecast_collection(_manifest(), repository, store, now_utc=NOW)

    assert plan.new_task_count == 1
    assert repository.list_forecasts() == ()
    with pytest.raises(ValueError, match="exceed max_new_tasks=0"):
        apply_forecast_plan(
            plan,
            repository,
            store,
            client=client,
            max_new_tasks=0,
            clock=lambda: NOW,
        )
    assert client.calls == 0
    assert repository.list_forecasts() == ()


def test_new_forecast_submission_is_blocked_while_time_contract_unverified(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    client = CountingClient()
    plan = plan_forecast_collection(_manifest(), repository, store, now_utc=NOW)

    with pytest.raises(
        ForecastSemanticsUnverifiedError, match="timezone used for start_date"
    ):
        apply_forecast_plan(
            plan,
            repository,
            store,
            client=client,
            max_new_tasks=1,
            clock=lambda: NOW,
        )
    assert client.calls == 0
    assert repository.list_forecasts() == ()


def test_exact_pre_target_cached_forecast_can_be_archived_without_new_task(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    request = _request()
    snapshot = store.publish(
        request,
        activity_id="cached-forecast",
        collected_at_utc=NOW,
        temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST,
        raw_result=_result(),
    )
    plan = plan_forecast_collection(
        _manifest(request),
        repository,
        store,
        now_utc=NOW + timedelta(minutes=1),
    )

    assert plan.items[0].action is ForecastAction.ARCHIVE_CACHED
    assert plan.new_task_count == 0
    created = apply_forecast_plan(
        plan,
        repository,
        store,
        client=None,
        max_new_tasks=0,
        clock=lambda: NOW + timedelta(minutes=1),
    )

    assert len(created) == 1
    assert created[0].requested_at_utc == snapshot.collected_at_utc
    assert created[0].assumed_target_valid_at_utc == datetime(
        2026, 8, 22, 15, tzinfo=UTC
    )
    assert created[0].per_tile_forecasts[0].forecast_temperature_c == 40.0


def test_realization_planner_never_looks_up_before_full_hour_matures(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository)
    store = NoLookupBeforeMaturityStore()

    plan = plan_realization_collection(
        repository,
        store,
        now_utc=datetime(2026, 8, 22, 15, 59, tzinfo=UTC),
    )

    assert plan.waiting_forecast_count == 1
    assert plan.items == ()
    assert store.calls == 0


def test_shared_request_waits_for_all_explicit_time_assumptions(tmp_path) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository, activity_id="utc-forecast")
    _record_forecast(
        repository,
        activity_id="offset-forecast",
        basis=RequestTimeBasis(
            assumption="Test-only UTC-7 assumption; not vendor-confirmed.",
            utc_offset_minutes=-7 * 60,
        ),
    )
    store = NoLookupBeforeMaturityStore()

    plan = plan_realization_collection(
        repository,
        store,
        now_utc=datetime(2026, 8, 22, 16, tzinfo=UTC),
    )

    assert plan.waiting_forecast_count == 2
    assert plan.items == ()
    assert store.calls == 0


def test_mature_forecast_counts_one_bounded_task_for_multiple_vintages(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository, activity_id="forecast-1")
    _record_forecast(
        repository,
        requested_at=NOW + timedelta(hours=1),
        activity_id="forecast-2",
    )
    store = HeatmapSnapshotStore(tmp_path / "snapshots")

    plan = plan_realization_collection(
        repository,
        store,
        now_utc=datetime(2026, 8, 22, 16, tzinfo=UTC),
    )

    assert plan.pending_forecast_count == 2
    assert plan.new_task_count == 1
    assert len(plan.items) == 1
    with pytest.raises(ValueError, match="exceed max_new_tasks=0"):
        apply_realization_plan(
            plan,
            repository,
            store,
            client=CountingClient(),
            max_new_tasks=0,
            clock=lambda: datetime(2026, 8, 22, 16, tzinfo=UTC),
        )


@pytest.mark.parametrize(
    "wrong_request",
    (
        _request(target_hour=14),
        _request(granularity=80),
        _request(west=-112.02),
    ),
    ids=("wrong-hour", "wrong-granularity", "wrong-aoi"),
)
def test_realization_cache_lookup_requires_the_exact_request(
    tmp_path, wrong_request
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository)
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    store.publish(
        wrong_request,
        activity_id="wrong-request",
        collected_at_utc=datetime(2026, 8, 22, 16, 5, tzinfo=UTC),
        temporal_scope=SnapshotTemporalScope.HISTORICAL,
        raw_result=_result(41.0),
    )

    plan = plan_realization_collection(
        repository,
        store,
        now_utc=datetime(2026, 8, 22, 16, 5, tzinfo=UTC),
    )

    assert plan.cached_request_count == 0
    assert plan.new_task_count == 1


def test_tampered_realization_plan_is_rejected_before_client_use(tmp_path) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository)
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    plan = plan_realization_collection(
        repository,
        store,
        now_utc=datetime(2026, 8, 22, 16, tzinfo=UTC),
    )
    tampered_item = replace(plan.items[0], request=_request(granularity=80))
    tampered = replace(plan, items=(tampered_item,))
    client = CountingClient()

    with pytest.raises(ValueError, match="fingerprint is inconsistent"):
        apply_realization_plan(
            tampered,
            repository,
            store,
            client=client,
            max_new_tasks=1,
            clock=lambda: datetime(2026, 8, 22, 16, tzinfo=UTC),
        )
    assert client.calls == 0


def test_cached_mature_result_attaches_only_as_vendor_relative_evidence(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    forecast = _record_forecast(repository, temperature=40.0)
    store = HeatmapSnapshotStore(tmp_path / "snapshots")
    collected = datetime(2026, 8, 22, 16, 5, tzinfo=UTC)
    store.publish(
        _request(),
        activity_id="later-vendor-1",
        collected_at_utc=collected,
        temporal_scope=SnapshotTemporalScope.HISTORICAL,
        raw_result=_result(41.25),
    )
    plan = plan_realization_collection(
        repository,
        store,
        now_utc=collected,
    )

    assert plan.new_task_count == 0
    created = apply_realization_plan(
        plan,
        repository,
        store,
        client=None,
        max_new_tasks=0,
        clock=lambda: collected,
    )

    assert len(created) == 1
    record = created[0]
    assert record.forecast_record_id == forecast.record_id
    assert record.residual_definition == "vendor_relative_realization_minus_forecast"
    assert record.mean_vendor_relative_residual_c == pytest.approx(1.25)
    assert record.recorded_at_utc >= forecast.assumed_target_valid_at_utc


def test_status_and_report_use_vendor_relative_not_ground_truth_labels(
    tmp_path,
) -> None:
    repository = ForecastPairRepository(tmp_path / "archive")
    _record_forecast(repository)
    now = datetime(2026, 8, 22, 16, tzinfo=UTC)

    status = build_archive_status(repository, now_utc=now)
    rows = build_vendor_relative_report(repository, now_utc=now)

    assert status.matured_without_realization == 1
    assert status.forecast_time_contract_status == "unverified_caller_assumption"
    assert status.new_api_submissions_enabled is False
    assert rows[0]["state"] == "vendor_relative_realization_due"
    assert rows[0]["forecast_time_contract_status"] == "unverified_caller_assumption"
    serialized = json.dumps(rows).lower()
    assert "ground_truth" not in serialized
    assert '"actual' not in serialized
    assert '"error' not in serialized
