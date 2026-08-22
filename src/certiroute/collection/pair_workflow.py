"""Fail-closed forecast-to-vendor-realization collection workflow.

The FortyGuard heatmap endpoint returns either forecast or historical output
depending on the requested wall clock.  Its public documentation currently
does not define the timezone used to interpret that wall clock.  Planning and
archive reporting are therefore available, but a task that would submit a new
API request is blocked until that contract is documented and verified.

Later FortyGuard output is deliberately called a *vendor-relative
realization*.  It is useful for measuring the vendor's forecast consistency;
it is not independent ground truth.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from certiroute.collection._json import normalize_json_object
from certiroute.collection.archive import ForecastArchive
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    forecast_record_id,
    heatmap_request_fingerprint,
)
from certiroute.collection.models import (
    ForecastRecord,
    RequestTimeBasis,
    TileForecast,
    VendorRelativeRealizationRecord,
    VendorRelativeTileValue,
    assumed_valid_at_utc,
    format_utc_datetime,
    require_utc_datetime,
)
from certiroute.collection.snapshot_cache import (
    HeatmapSnapshot,
    HeatmapSnapshotStore,
    SnapshotTemporalScope,
)
from certiroute.fortyguard.geometry import validate_aoi_area
from certiroute.fortyguard.heatmap_profiles import extract_heatmap_tiles
from certiroute.fortyguard.schemas import HeatmapRequest

MAX_DOCUMENTED_FORECAST_HORIZON = timedelta(hours=12)
SINGLE_HOUR_WINDOW = timedelta(hours=1)
DEFAULT_REALIZATION_SETTLING_DELAY = SINGLE_HOUR_WINDOW

# Do not turn this into a CLI switch.  It may change only after the official
# contract states how a heatmap request wall clock maps to a timezone and that
# behavior has been verified against a response carrying matching time metadata.
_OFFICIAL_FORECAST_TIME_CONTRACT_VERIFIED = False
_UNVERIFIED_CONTRACT_MESSAGE = (
    "live FortyGuard forecast-pair submission is disabled: the official heatmap "
    "documentation allows future wall-clock requests but does not state the "
    "timezone used for start_date/start_time or echo a forecast valid-time in the "
    "result. Planning, archive status, reports, and exact cached attachments remain "
    "explicitly assumption-qualified; obtain a written vendor contract and add a "
    "verified integration test "
    "before enabling POST /v1/heatmap here"
)


class ForecastSemanticsUnverifiedError(RuntimeError):
    """A new API submission was blocked because forecast time is ambiguous."""


class HeatmapCreator(Protocol):
    """Small client boundary used by the collection executor."""

    def create_heatmap(
        self,
        request: HeatmapRequest,
        *,
        poll_interval_seconds: float = 2.0,
        max_attempts: int = 60,
    ) -> tuple[str, Mapping[str, Any]]: ...


class ForecastPairManifest(BaseModel):
    """Exact requests plus the caller's explicit wall-clock assumption."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_schema_version: Literal[1] = 1
    request_time_basis: RequestTimeBasis
    requests: tuple[HeatmapRequest, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def reject_duplicate_requests(self) -> ForecastPairManifest:
        fingerprints = [heatmap_request_fingerprint(item) for item in self.requests]
        if len(set(fingerprints)) != len(fingerprints):
            raise ValueError("manifest requests must be unique exact heatmap requests")
        return self


class ForecastAction(StrEnum):
    """Planned handling for one future exact request."""

    ALREADY_ARCHIVED = "already_archived"
    ARCHIVE_CACHED = "archive_cached"
    SUBMIT_NEW_TASK = "submit_new_task"


@dataclass(frozen=True)
class ForecastPlanItem:
    """One validated future target and its planned evidence source."""

    request: HeatmapRequest
    request_fingerprint: str
    request_time_basis: RequestTimeBasis
    assumed_target_valid_at_utc: datetime
    action: ForecastAction
    cached_snapshot: HeatmapSnapshot | None = None
    existing_forecast_id: str | None = None


@dataclass(frozen=True)
class ForecastCollectionPlan:
    """Dry-run-safe plan for future forecast vintages."""

    planned_at_utc: datetime
    items: tuple[ForecastPlanItem, ...]
    store_validation_token: object

    @property
    def new_task_count(self) -> int:
        return sum(item.action is ForecastAction.SUBMIT_NEW_TASK for item in self.items)

    @property
    def cached_archive_count(self) -> int:
        return sum(item.action is ForecastAction.ARCHIVE_CACHED for item in self.items)

    @property
    def already_archived_count(self) -> int:
        return sum(
            item.action is ForecastAction.ALREADY_ARCHIVED for item in self.items
        )


@dataclass(frozen=True)
class RealizationPlanItem:
    """Mature forecast vintages sharing one exact later-vendor request."""

    request: HeatmapRequest
    request_fingerprint: str
    forecasts: tuple[ForecastRecord, ...]
    earliest_allowed_at_utc: datetime
    cached_snapshot: HeatmapSnapshot | None = None

    @property
    def needs_new_task(self) -> bool:
        return self.cached_snapshot is None


@dataclass(frozen=True)
class RealizationCollectionPlan:
    """Dry-run-safe plan that never looks up an immature target."""

    planned_at_utc: datetime
    settling_delay: timedelta
    items: tuple[RealizationPlanItem, ...]
    waiting_forecast_count: int
    paired_forecast_count: int
    store_validation_token: object

    @property
    def new_task_count(self) -> int:
        return sum(item.needs_new_task for item in self.items)

    @property
    def cached_request_count(self) -> int:
        return len(self.items) - self.new_task_count

    @property
    def pending_forecast_count(self) -> int:
        return sum(len(item.forecasts) for item in self.items)


@dataclass(frozen=True)
class PairArchiveStatus:
    """Machine-readable state counts for recurring collection jobs."""

    generated_at_utc: str
    total_forecast_vintages: int
    waiting_for_realization: int
    matured_without_realization: int
    paired_forecast_vintages: int
    vendor_relative_realization_vintages: int
    forecast_time_contract_status: Literal["unverified_caller_assumption"]
    new_api_submissions_enabled: bool


class ForecastPairRepository:
    """Inventory facade over the existing append-only archive/cache classes."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.archive = ForecastArchive(self.root)
        self._forecast_inventory = JsonDiskCache(self.root / "forecasts")

    def list_forecasts(self) -> tuple[ForecastRecord, ...]:
        records: list[ForecastRecord] = []
        for identifier in self._forecast_inventory.record_ids():
            record = self.archive.get_forecast(identifier)
            if record is None:
                raise CacheCorruptionError(
                    f"forecast inventory references a missing record: {identifier}"
                )
            records.append(record)
        return tuple(
            sorted(records, key=lambda item: (item.requested_at_utc, item.record_id))
        )


def load_manifest(path: str | Path) -> ForecastPairManifest:
    """Read one secret-free, exact-request JSON manifest."""

    manifest_path = Path(path)
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"could not read manifest: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"manifest is not valid JSON: {manifest_path}") from exc
    try:
        safe_raw = normalize_json_object(raw, path="$manifest")
        return ForecastPairManifest.model_validate(safe_raw)
    except (TypeError, ValidationError) as exc:
        raise ValueError(f"invalid forecast-pair manifest: {exc}") from exc


def plan_forecast_collection(
    manifest: ForecastPairManifest,
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
    *,
    now_utc: datetime | None = None,
) -> ForecastCollectionPlan:
    """Plan future vintages without writing data or calling FortyGuard."""

    now = _utc_now(now_utc)
    requests = tuple(manifest.requests)
    targets: dict[str, datetime] = {}
    for request in requests:
        normalize_json_object(
            request.model_dump(mode="json", exclude_none=True),
            path="$manifest.requests[]",
        )
        validate_aoi_area(request.polygon_aoi)
        if (
            request.date_time.start_time.second
            or request.date_time.start_time.microsecond
        ):
            raise ValueError("heatmap request time precision must be one minute")
        fingerprint = heatmap_request_fingerprint(request)
        target = _assumed_target(request, manifest.request_time_basis)
        if target <= now:
            raise ValueError(
                "forecast target must be strictly after the planning instant: "
                f"{format_utc_datetime(target)}"
            )
        if target - now > MAX_DOCUMENTED_FORECAST_HORIZON:
            raise ValueError(
                "forecast target exceeds FortyGuard's documented 12-hour horizon: "
                f"{format_utc_datetime(target)}"
            )
        targets[fingerprint] = target

    candidates = store.list_for_requests(
        requests,
        temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST,
    )
    items: list[ForecastPlanItem] = []
    for request in requests:
        fingerprint = heatmap_request_fingerprint(request)
        target = targets[fingerprint]
        eligible = [
            snapshot
            for snapshot in candidates[fingerprint]
            if snapshot.collected_at_utc <= now and snapshot.collected_at_utc < target
        ]
        cached = eligible[-1] if eligible else None
        existing_id = None
        action = ForecastAction.SUBMIT_NEW_TASK
        if cached is not None:
            candidate_id = forecast_record_id(
                fingerprint,
                cached.collected_at_utc,
                cached.activity_id,
            )
            if repository.archive.get_forecast(candidate_id) is not None:
                existing_id = candidate_id
                action = ForecastAction.ALREADY_ARCHIVED
            else:
                action = ForecastAction.ARCHIVE_CACHED
        items.append(
            ForecastPlanItem(
                request=request,
                request_fingerprint=fingerprint,
                request_time_basis=manifest.request_time_basis,
                assumed_target_valid_at_utc=target,
                action=action,
                cached_snapshot=cached,
                existing_forecast_id=existing_id,
            )
        )
    return ForecastCollectionPlan(
        planned_at_utc=now,
        items=tuple(items),
        store_validation_token=store.validation_token,
    )


def apply_forecast_plan(
    plan: ForecastCollectionPlan,
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    max_new_tasks: int,
    poll_interval_seconds: float = 2.0,
    max_attempts: int = 60,
    clock: Callable[[], datetime] | None = None,
) -> tuple[ForecastRecord, ...]:
    """Archive cached vintages and, once verified, execute a bounded live plan."""

    _validate_cap(max_new_tasks)
    _validate_store_token(plan.store_validation_token, store)
    _validate_forecast_plan(plan, repository)
    if plan.new_task_count > max_new_tasks:
        raise ValueError(
            f"{plan.new_task_count} new tasks exceed max_new_tasks={max_new_tasks}"
        )
    if plan.new_task_count:
        require_verified_forecast_time_contract()
        if client is None:
            raise ValueError("client is required when the plan has new tasks")

    get_now = clock or (lambda: datetime.now(UTC))
    execution_started = _utc_now(get_now())
    if any(
        item.assumed_target_valid_at_utc <= execution_started for item in plan.items
    ):
        raise ValueError("forecast plan expired because a target is no longer future")

    created: list[ForecastRecord] = []
    for item in plan.items:
        if item.action is ForecastAction.ALREADY_ARCHIVED:
            continue
        snapshot = item.cached_snapshot
        from_cache = snapshot is not None
        requested_at = snapshot.collected_at_utc if snapshot is not None else None
        if snapshot is None:
            request_started = _utc_now(get_now())
            if request_started >= item.assumed_target_valid_at_utc:
                raise ValueError("forecast target passed before API submission")
            assert client is not None  # guarded above
            activity_id, raw_result = client.create_heatmap(
                item.request,
                poll_interval_seconds=poll_interval_seconds,
                max_attempts=max_attempts,
            )
            completed_at = _utc_now(get_now())
            if completed_at < request_started:
                raise ValueError("collection clock moved backwards during API task")
            if completed_at >= item.assumed_target_valid_at_utc:
                raise ValueError(
                    "forecast result completed at or after its target; refusing to "
                    "archive lookahead-contaminated evidence"
                )
            snapshot = store.publish(
                item.request,
                activity_id=activity_id,
                collected_at_utc=completed_at,
                temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST,
                raw_result=raw_result,
            )
            requested_at = request_started
        _validate_snapshot_for_request(snapshot, item.request)
        if from_cache and snapshot.collected_at_utc > execution_started:
            raise ValueError("cached forecast snapshot is dated after execution time")
        if snapshot.collected_at_utc >= item.assumed_target_valid_at_utc:
            raise ValueError(
                "cached forecast was collected at or after its target; refusing "
                "lookahead-contaminated evidence"
            )
        assert requested_at is not None
        identifier = forecast_record_id(
            item.request_fingerprint, requested_at, snapshot.activity_id
        )
        if repository.archive.get_forecast(identifier) is not None:
            continue
        try:
            record = repository.archive.record_forecast(
                item.request,
                requested_at_utc=requested_at,
                request_time_basis=item.request_time_basis,
                activity_id=snapshot.activity_id,
                per_tile_forecasts=_forecast_tiles(snapshot.raw_result),
                raw_forecast_result=snapshot.raw_result,
            )
        except FileExistsError:
            if repository.archive.get_forecast(identifier) is None:
                raise
        else:
            created.append(record)
    return tuple(created)


def plan_realization_collection(
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
    *,
    now_utc: datetime | None = None,
    settling_delay: timedelta = DEFAULT_REALIZATION_SETTLING_DELAY,
) -> RealizationCollectionPlan:
    """Plan exact later-vendor requests, looking up only matured targets."""

    now = _utc_now(now_utc)
    delay = _validate_settling_delay(settling_delay)
    unpaired: dict[str, list[ForecastRecord]] = defaultdict(list)
    paired = 0
    for forecast in repository.list_forecasts():
        if repository.archive.latest_vendor_relative_realization(forecast.record_id):
            paired += 1
            continue
        unpaired[forecast.request_fingerprint].append(forecast)

    grouped: dict[str, list[ForecastRecord]] = {}
    waiting = 0
    for fingerprint, forecasts in unpaired.items():
        # One exact vendor request may have forecast vintages with different
        # caller-supplied timezone assumptions. Do not query that request until
        # every associated target is mature; otherwise the lookup would create
        # lookahead for the later interpretation.
        if any(
            now < forecast.assumed_target_valid_at_utc + delay for forecast in forecasts
        ):
            waiting += len(forecasts)
            continue
        grouped[fingerprint] = forecasts

    # This lookup intentionally happens only after every immature forecast has
    # been filtered out.  In particular, an all-immature archive never touches
    # the realization snapshot store.
    requests = {
        fingerprint: _request_from_forecast(forecasts[0])
        for fingerprint, forecasts in grouped.items()
    }
    candidates = (
        store.list_for_requests(
            requests.values(), temporal_scope=SnapshotTemporalScope.HISTORICAL
        )
        if requests
        else {}
    )

    items: list[RealizationPlanItem] = []
    for fingerprint, forecasts in sorted(grouped.items()):
        request = requests[fingerprint]
        if heatmap_request_fingerprint(request) != fingerprint:
            raise CacheCorruptionError(
                "forecast request fields do not match the archived fingerprint"
            )
        earliest = max(
            forecast.assumed_target_valid_at_utc + delay for forecast in forecasts
        )
        eligible = [
            snapshot
            for snapshot in candidates[fingerprint]
            if earliest <= snapshot.collected_at_utc <= now
        ]
        items.append(
            RealizationPlanItem(
                request=request,
                request_fingerprint=fingerprint,
                forecasts=tuple(forecasts),
                earliest_allowed_at_utc=earliest,
                cached_snapshot=eligible[-1] if eligible else None,
            )
        )
    return RealizationCollectionPlan(
        planned_at_utc=now,
        settling_delay=delay,
        items=tuple(items),
        waiting_forecast_count=waiting,
        paired_forecast_count=paired,
        store_validation_token=store.validation_token,
    )


def apply_realization_plan(
    plan: RealizationCollectionPlan,
    repository: ForecastPairRepository,
    store: HeatmapSnapshotStore,
    *,
    client: HeatmapCreator | None,
    max_new_tasks: int,
    poll_interval_seconds: float = 2.0,
    max_attempts: int = 60,
    clock: Callable[[], datetime] | None = None,
) -> tuple[VendorRelativeRealizationRecord, ...]:
    """Attach exact vendor-relative realizations under a strict task cap."""

    _validate_cap(max_new_tasks)
    _validate_store_token(plan.store_validation_token, store)
    _validate_realization_plan(plan, repository)
    if plan.new_task_count > max_new_tasks:
        raise ValueError(
            f"{plan.new_task_count} new tasks exceed max_new_tasks={max_new_tasks}"
        )
    if plan.new_task_count:
        require_verified_forecast_time_contract()
        if client is None:
            raise ValueError("client is required when the plan has new tasks")

    get_now = clock or (lambda: datetime.now(UTC))
    execution_now = _utc_now(get_now())
    if any(execution_now < item.earliest_allowed_at_utc for item in plan.items):
        raise ValueError("realization plan contains a target that has not matured")

    created: list[VendorRelativeRealizationRecord] = []
    for item in plan.items:
        snapshot = item.cached_snapshot
        from_cache = snapshot is not None
        if snapshot is None:
            assert client is not None  # guarded above
            activity_id, raw_result = client.create_heatmap(
                item.request,
                poll_interval_seconds=poll_interval_seconds,
                max_attempts=max_attempts,
            )
            completed_at = _utc_now(get_now())
            if completed_at < item.earliest_allowed_at_utc:
                raise ValueError(
                    "later-vendor result arrived before the forecast window matured"
                )
            snapshot = store.publish(
                item.request,
                activity_id=activity_id,
                collected_at_utc=completed_at,
                temporal_scope=SnapshotTemporalScope.HISTORICAL,
                raw_result=raw_result,
            )
        _validate_snapshot_for_request(snapshot, item.request)
        if from_cache and snapshot.collected_at_utc > execution_now:
            raise ValueError(
                "cached later-vendor snapshot is dated after execution time"
            )
        if snapshot.collected_at_utc < item.earliest_allowed_at_utc:
            raise ValueError("cached later-vendor result predates forecast maturity")
        values = _realization_tiles(snapshot.raw_result)
        realized_keys = {value.spatial_key for value in values}
        for forecast in item.forecasts:
            forecast_keys = {value.spatial_key for value in forecast.per_tile_forecasts}
            if forecast_keys != realized_keys:
                raise ValueError(
                    "later-vendor tiles do not exactly match forecast geometry for "
                    f"{forecast.record_id}"
                )
        for forecast in item.forecasts:
            if repository.archive.latest_vendor_relative_realization(
                forecast.record_id
            ):
                continue
            try:
                record = repository.archive.record_vendor_relative_realization(
                    forecast.record_id,
                    request=item.request,
                    request_time_basis=forecast.request_time_basis,
                    recorded_at_utc=snapshot.collected_at_utc,
                    activity_id=snapshot.activity_id,
                    per_tile_realizations=values,
                    raw_result=snapshot.raw_result,
                )
            except FileExistsError:
                if not repository.archive.list_vendor_relative_realizations(
                    forecast.record_id
                ):
                    raise
            else:
                created.append(record)
    return tuple(created)


def build_archive_status(
    repository: ForecastPairRepository,
    *,
    now_utc: datetime | None = None,
    settling_delay: timedelta = DEFAULT_REALIZATION_SETTLING_DELAY,
) -> PairArchiveStatus:
    """Summarize pair state without accessing the network or snapshot cache."""

    now = _utc_now(now_utc)
    delay = _validate_settling_delay(settling_delay)
    waiting = 0
    matured = 0
    paired = 0
    realization_count = 0
    forecasts = repository.list_forecasts()
    for forecast in forecasts:
        realizations = repository.archive.list_vendor_relative_realizations(
            forecast.record_id
        )
        realization_count += len(realizations)
        if realizations:
            paired += 1
        elif now < forecast.assumed_target_valid_at_utc + delay:
            waiting += 1
        else:
            matured += 1
    return PairArchiveStatus(
        generated_at_utc=format_utc_datetime(now),
        total_forecast_vintages=len(forecasts),
        waiting_for_realization=waiting,
        matured_without_realization=matured,
        paired_forecast_vintages=paired,
        vendor_relative_realization_vintages=realization_count,
        forecast_time_contract_status="unverified_caller_assumption",
        new_api_submissions_enabled=_OFFICIAL_FORECAST_TIME_CONTRACT_VERIFIED,
    )


def build_vendor_relative_report(
    repository: ForecastPairRepository,
    *,
    now_utc: datetime | None = None,
    settling_delay: timedelta = DEFAULT_REALIZATION_SETTLING_DELAY,
) -> tuple[dict[str, Any], ...]:
    """Return audit rows whose labels never imply independent ground truth."""

    now = _utc_now(now_utc)
    delay = _validate_settling_delay(settling_delay)
    rows: list[dict[str, Any]] = []
    for forecast in repository.list_forecasts():
        realization = repository.archive.latest_vendor_relative_realization(
            forecast.record_id
        )
        if realization is not None:
            state = "vendor_relative_pair_complete"
        elif now < forecast.assumed_target_valid_at_utc + delay:
            state = "waiting_for_vendor_relative_realization"
        else:
            state = "vendor_relative_realization_due"
        residuals = (
            [item.vendor_relative_residual_c for item in realization.per_tile_residuals]
            if realization is not None
            else []
        )
        rows.append(
            {
                "forecast_record_id": forecast.record_id,
                "request_fingerprint": forecast.request_fingerprint,
                "forecast_requested_at_utc": format_utc_datetime(
                    forecast.requested_at_utc
                ),
                "forecast_assumed_target_valid_at_utc": format_utc_datetime(
                    forecast.assumed_target_valid_at_utc
                ),
                "forecast_assumed_lead_hours": forecast.assumed_lead_hours,
                "request_time_assumption": forecast.request_time_basis.assumption,
                "request_time_utc_offset_minutes": (
                    forecast.request_time_basis.utc_offset_minutes
                ),
                "forecast_time_contract_status": ("unverified_caller_assumption"),
                "granularity_m": forecast.granularity,
                "state": state,
                "vendor_relative_realization_record_id": (
                    realization.record_id if realization is not None else None
                ),
                "vendor_relative_realization_recorded_at_utc": (
                    format_utc_datetime(realization.recorded_at_utc)
                    if realization is not None
                    else None
                ),
                "mean_vendor_relative_residual_c": (
                    realization.mean_vendor_relative_residual_c
                    if realization is not None
                    else None
                ),
                "mean_absolute_vendor_relative_residual_c": (
                    sum(abs(value) for value in residuals) / len(residuals)
                    if residuals
                    else None
                ),
                "tile_count": len(forecast.per_tile_forecasts),
            }
        )
    return tuple(rows)


def status_as_dict(status: PairArchiveStatus) -> dict[str, Any]:
    """Serialize a status dataclass for stable scheduled-job JSON output."""

    return asdict(status)


def require_verified_forecast_time_contract() -> None:
    """Fail before any live POST while request-time semantics are undocumented."""

    if not _OFFICIAL_FORECAST_TIME_CONTRACT_VERIFIED:
        raise ForecastSemanticsUnverifiedError(_UNVERIFIED_CONTRACT_MESSAGE)


def _forecast_tiles(result: Mapping[str, Any]) -> tuple[TileForecast, ...]:
    return tuple(
        TileForecast(
            geometry=geometry,
            forecast_temperature_c=temperature,
            vendor_tile_id=vendor_tile_id,
        )
        for geometry, temperature, vendor_tile_id in _strict_tile_values(result)
    )


def _realization_tiles(
    result: Mapping[str, Any],
) -> tuple[VendorRelativeTileValue, ...]:
    return tuple(
        VendorRelativeTileValue(
            geometry=geometry,
            vendor_relative_realization_temperature_c=temperature,
            vendor_tile_id=vendor_tile_id,
        )
        for geometry, temperature, vendor_tile_id in _strict_tile_values(result)
    )


def _strict_tile_values(
    result: Mapping[str, Any],
) -> tuple[tuple[Mapping[str, Any], float, str | None], ...]:
    tiles = extract_heatmap_tiles(result)
    map_data = result["map_data"]
    assert isinstance(map_data, Mapping)  # validated by extract_heatmap_tiles
    features = map_data["features"]
    assert isinstance(features, Sequence)  # validated by extract_heatmap_tiles
    values: list[tuple[Mapping[str, Any], float, str | None]] = []
    for tile in tiles:
        feature = features[tile.feature_index]
        assert isinstance(feature, Mapping)
        geometry = feature["geometry"]
        assert isinstance(geometry, Mapping)
        values.append((geometry, tile.average_temperature_c, tile.vendor_tile_id))
    return tuple(values)


def _request_from_forecast(forecast: ForecastRecord) -> HeatmapRequest:
    request = HeatmapRequest.model_validate(
        {
            "polygon_aoi": forecast.aoi,
            "date_time": {
                "start_date": forecast.request_start_date.isoformat(),
                "start_time": forecast.request_start_time.strftime("%H:%M"),
                "filter_type": 1,
            },
            "granularity": forecast.granularity,
            "analytic_type": forecast.analytic_type,
        }
    )
    if heatmap_request_fingerprint(request) != forecast.request_fingerprint:
        raise CacheCorruptionError(
            "reconstructed forecast request fingerprint is inconsistent"
        )
    return request


def _assumed_target(
    request: HeatmapRequest, request_time_basis: RequestTimeBasis
) -> datetime:
    return assumed_valid_at_utc(
        request.date_time.start_date,
        request.date_time.start_time,
        request_time_basis,
    )


def _validate_snapshot_for_request(
    snapshot: HeatmapSnapshot, request: HeatmapRequest
) -> None:
    if snapshot.request_fingerprint != heatmap_request_fingerprint(request):
        raise ValueError("snapshot does not match the exact heatmap request")


def _validate_forecast_plan(
    plan: ForecastCollectionPlan, repository: ForecastPairRepository
) -> None:
    if not plan.items:
        raise ValueError("forecast collection plan must contain at least one item")
    planned_at = require_utc_datetime(
        plan.planned_at_utc, field_name="plan.planned_at_utc"
    )
    fingerprints: list[str] = []
    for item in plan.items:
        expected_fingerprint = heatmap_request_fingerprint(item.request)
        if item.request_fingerprint != expected_fingerprint:
            raise ValueError("forecast plan request fingerprint is inconsistent")
        fingerprints.append(expected_fingerprint)
        expected_target = _assumed_target(item.request, item.request_time_basis)
        if item.assumed_target_valid_at_utc != expected_target:
            raise ValueError("forecast plan target is inconsistent with its request")
        if expected_target <= planned_at:
            raise ValueError("forecast plan target must be future at planning time")
        if expected_target - planned_at > MAX_DOCUMENTED_FORECAST_HORIZON:
            raise ValueError("forecast plan target exceeds the documented horizon")
        if item.action is ForecastAction.SUBMIT_NEW_TASK:
            if (
                item.cached_snapshot is not None
                or item.existing_forecast_id is not None
            ):
                raise ValueError(
                    "new-task forecast item cannot contain cached evidence"
                )
            continue
        if item.cached_snapshot is None:
            raise ValueError("cached forecast action requires an exact snapshot")
        _validate_snapshot_for_request(item.cached_snapshot, item.request)
        if (
            item.cached_snapshot.temporal_scope
            is not SnapshotTemporalScope.CURRENT_OR_FORECAST
        ):
            raise ValueError("forecast evidence must use current-or-forecast scope")
        if item.cached_snapshot.collected_at_utc >= expected_target:
            raise ValueError("forecast evidence must have been collected before target")
        if item.cached_snapshot.collected_at_utc > planned_at:
            raise ValueError("forecast evidence is dated after planning time")
        expected_record_id = forecast_record_id(
            expected_fingerprint,
            item.cached_snapshot.collected_at_utc,
            item.cached_snapshot.activity_id,
        )
        if item.action is ForecastAction.ALREADY_ARCHIVED:
            if item.existing_forecast_id != expected_record_id:
                raise ValueError("already-archived forecast ID is inconsistent")
            if repository.archive.get_forecast(expected_record_id) is None:
                raise ValueError("already-archived forecast record is missing")
        elif item.existing_forecast_id is not None:
            raise ValueError("unarchived cached forecast cannot have an existing ID")
    if len(set(fingerprints)) != len(fingerprints):
        raise ValueError("forecast plan contains duplicate exact requests")


def _validate_realization_plan(
    plan: RealizationCollectionPlan, repository: ForecastPairRepository
) -> None:
    planned_at = require_utc_datetime(
        plan.planned_at_utc, field_name="plan.planned_at_utc"
    )
    delay = _validate_settling_delay(plan.settling_delay)
    if plan.waiting_forecast_count < 0 or plan.paired_forecast_count < 0:
        raise ValueError("realization plan counts cannot be negative")
    request_fingerprints: list[str] = []
    forecast_ids: list[str] = []
    for item in plan.items:
        if not item.forecasts:
            raise ValueError("realization plan item must contain forecast vintages")
        expected_fingerprint = heatmap_request_fingerprint(item.request)
        if item.request_fingerprint != expected_fingerprint:
            raise ValueError("realization plan request fingerprint is inconsistent")
        request_fingerprints.append(expected_fingerprint)
        expected_earliest = max(
            forecast.assumed_target_valid_at_utc + delay for forecast in item.forecasts
        )
        if item.earliest_allowed_at_utc != expected_earliest:
            raise ValueError("realization plan maturity time is inconsistent")
        if expected_earliest > planned_at:
            raise ValueError("realization plan contains an immature forecast")
        for forecast in item.forecasts:
            forecast_ids.append(forecast.record_id)
            if repository.archive.get_forecast(forecast.record_id) != forecast:
                raise ValueError("realization plan forecast is not in this archive")
            if forecast.request_fingerprint != expected_fingerprint:
                raise ValueError(
                    "realization forecast request fingerprint is mismatched"
                )
            if (
                heatmap_request_fingerprint(_request_from_forecast(forecast))
                != expected_fingerprint
            ):
                raise ValueError("realization forecast request fields are mismatched")
        if item.cached_snapshot is None:
            continue
        _validate_snapshot_for_request(item.cached_snapshot, item.request)
        if item.cached_snapshot.temporal_scope is not SnapshotTemporalScope.HISTORICAL:
            raise ValueError("later-vendor evidence must use historical scope")
        if item.cached_snapshot.collected_at_utc < expected_earliest:
            raise ValueError("later-vendor evidence predates forecast maturity")
        if item.cached_snapshot.collected_at_utc > planned_at:
            raise ValueError("later-vendor evidence is dated after planning time")
    if len(set(request_fingerprints)) != len(request_fingerprints):
        raise ValueError("realization plan contains duplicate exact requests")
    if len(set(forecast_ids)) != len(forecast_ids):
        raise ValueError("realization plan contains duplicate forecast vintages")


def _validate_cap(max_new_tasks: int) -> None:
    if isinstance(max_new_tasks, bool) or not isinstance(max_new_tasks, int):
        raise TypeError("max_new_tasks must be an integer")
    if max_new_tasks < 0:
        raise ValueError("max_new_tasks cannot be negative")


def _validate_store_token(token: object, store: HeatmapSnapshotStore) -> None:
    if token is not store.validation_token:
        raise ValueError("collection plan belongs to a different snapshot store")


def _validate_settling_delay(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta):
        raise TypeError("settling_delay must be a timedelta")
    if value < SINGLE_HOUR_WINDOW:
        raise ValueError(
            "settling_delay must cover the complete one-hour forecast window"
        )
    return value


def _utc_now(value: datetime | None) -> datetime:
    return require_utc_datetime(
        value if value is not None else datetime.now(UTC), field_name="now_utc"
    )


__all__ = [
    "DEFAULT_REALIZATION_SETTLING_DELAY",
    "ForecastAction",
    "ForecastCollectionPlan",
    "ForecastPairManifest",
    "ForecastPairRepository",
    "ForecastPlanItem",
    "ForecastSemanticsUnverifiedError",
    "PairArchiveStatus",
    "RealizationCollectionPlan",
    "RealizationPlanItem",
    "apply_forecast_plan",
    "apply_realization_plan",
    "build_archive_status",
    "build_vendor_relative_report",
    "load_manifest",
    "plan_forecast_collection",
    "plan_realization_collection",
    "require_verified_forecast_time_contract",
    "status_as_dict",
]
