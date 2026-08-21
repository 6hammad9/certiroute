"""Memoizing, append-only cache for completed FortyGuard heatmap snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)

from certiroute.collection._json import normalize_json_object
from certiroute.collection.cache import CacheCorruptionError, JsonDiskCache
from certiroute.collection.fingerprints import (
    coerce_heatmap_request,
    heatmap_request_fingerprint,
    normalize_heatmap_request,
)
from certiroute.collection.models import format_utc_datetime, require_utc_datetime
from certiroute.fortyguard.schemas import HeatmapRequest

DEFAULT_SNAPSHOT_CACHE_ROOT = Path("data/raw/fortyguard_heatmap_snapshots")


class SnapshotTemporalScope(StrEnum):
    """Caller-declared reuse semantics for a completed heatmap result."""

    HISTORICAL = "historical"
    CURRENT_OR_FORECAST = "current_or_forecast"


class HeatmapSnapshot(BaseModel):
    """One immutable completed API result and its integrity metadata."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_schema_version: Literal[1] = 1
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    temporal_scope: SnapshotTemporalScope
    completion_status: Literal["completed"] = "completed"
    activity_id: str = Field(min_length=1)
    collected_at_utc: datetime
    request_contract: dict[str, Any]
    raw_result: dict[str, Any]
    content_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("activity_id")
    @classmethod
    def normalize_activity_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("activity_id cannot be blank")
        return normalized

    @field_validator("collected_at_utc", mode="before")
    @classmethod
    def normalize_collected_at(cls, value: Any) -> datetime:
        return require_utc_datetime(value, field_name="collected_at_utc")

    @field_serializer("collected_at_utc", when_used="json")
    def serialize_collected_at(self, value: datetime) -> str:
        return format_utc_datetime(value)

    @field_validator("request_contract", mode="before")
    @classmethod
    def validate_request_contract(cls, value: Any) -> dict[str, Any]:
        return normalize_json_object(value, path="$.request_contract")

    @field_validator("raw_result", mode="before")
    @classmethod
    def validate_raw_result(cls, value: Any) -> dict[str, Any]:
        result = normalize_json_object(value, path="$.raw_result")
        if not result:
            raise ValueError("raw_result must contain a completed heatmap result")
        return result

    @model_validator(mode="after")
    def validate_integrity(self) -> HeatmapSnapshot:
        try:
            request = HeatmapRequest.model_validate(self.request_contract)
        except ValidationError as exc:
            raise ValueError("request_contract is not a valid HeatmapRequest") from exc
        expected_fingerprint = heatmap_request_fingerprint(request)
        if self.request_fingerprint != expected_fingerprint:
            raise ValueError(
                "request_fingerprint does not match the persisted request contract"
            )
        expected_id = heatmap_snapshot_id(
            self.request_fingerprint,
            self.collected_at_utc,
            self.activity_id,
        )
        if self.snapshot_id != expected_id:
            raise ValueError("snapshot_id does not match its immutable identity")
        checksum_payload = self.model_dump(
            mode="json", exclude={"content_checksum_sha256"}
        )
        expected_checksum = _json_sha256(checksum_payload)
        if self.content_checksum_sha256 != expected_checksum:
            raise ValueError("snapshot content checksum does not match its payload")
        return self


class HeatmapSnapshotIndexPointer(BaseModel):
    """Small, checksummed pointer from a request fingerprint to a snapshot."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index_schema_version: Literal[2] = 2
    snapshot_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_content_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_file_checksum_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class HeatmapSnapshotStore:
    """Append and safely memoize completed heatmap results.

    Historical reuse is deliberately separate from current/forecast reuse.
    Current or forecast snapshots are returned only through an explicit positive
    TTL, preventing an accidental indefinite cache lifetime.
    """

    def __init__(
        self,
        root: str | Path = DEFAULT_SNAPSHOT_CACHE_ROOT,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._cache = JsonDiskCache(root, clock=self._clock)
        self._request_index_root = self._cache.root / ".request_index"
        self._validation_token = object()

    @property
    def root(self) -> Path:
        """Resolved storage root; the default is under Git-ignored ``data/raw``."""

        return self._cache.root

    @property
    def validation_token(self) -> object:
        """Opaque identity for plans validated by this in-process store."""

        return self._validation_token

    def publish(
        self,
        request: HeatmapRequest | Mapping[str, Any],
        *,
        activity_id: str,
        collected_at_utc: datetime,
        temporal_scope: SnapshotTemporalScope | str,
        raw_result: Mapping[str, Any],
    ) -> HeatmapSnapshot:
        """Append one completed result; an identical identity never overwrites."""

        validated_request = coerce_heatmap_request(request)
        contract = normalize_json_object(
            normalize_heatmap_request(validated_request),
            path="$.request_contract",
        )
        result = normalize_json_object(raw_result, path="$.raw_result")
        if not result:
            raise ValueError("raw_result must contain a completed heatmap result")
        collected = require_utc_datetime(
            collected_at_utc, field_name="collected_at_utc"
        )
        normalized_activity_id = activity_id.strip()
        if not normalized_activity_id:
            raise ValueError("activity_id cannot be blank")
        scope = SnapshotTemporalScope(temporal_scope)
        fingerprint = heatmap_request_fingerprint(validated_request)
        identifier = heatmap_snapshot_id(fingerprint, collected, normalized_activity_id)
        payload: dict[str, Any] = {
            "snapshot_schema_version": 1,
            "snapshot_id": identifier,
            "request_fingerprint": fingerprint,
            "temporal_scope": scope.value,
            "completion_status": "completed",
            "activity_id": normalized_activity_id,
            "collected_at_utc": format_utc_datetime(collected),
            "request_contract": contract,
            "raw_result": result,
        }
        payload["content_checksum_sha256"] = _json_sha256(payload)
        snapshot = HeatmapSnapshot.model_validate(payload)
        path = self._cache.add(identifier, snapshot.model_dump(mode="json"))
        try:
            self._publish_index_pointer(snapshot, snapshot_path=path)
        except Exception:
            # The completed API result is already durably published. Returning
            # it prevents a caller from resubmitting a credit-consuming task
            # because an auxiliary pointer could not be written. The next
            # lookup detects and strictly backfills the unindexed main record.
            pass
        return snapshot

    def get(self, snapshot_id: str) -> HeatmapSnapshot | None:
        """Load one explicit snapshot and verify all integrity fields."""

        payload = self._cache.get(snapshot_id)
        if payload is None:
            return None
        try:
            snapshot = HeatmapSnapshot.model_validate(payload)
        except ValidationError as exc:
            raise CacheCorruptionError(
                f"invalid heatmap snapshot payload: {snapshot_id}"
            ) from exc
        if snapshot.snapshot_id != snapshot_id:
            raise CacheCorruptionError(
                "heatmap snapshot ID does not match its cache key"
            )
        return snapshot

    def list_for_request(
        self,
        request: HeatmapRequest | Mapping[str, Any] | str,
        *,
        temporal_scope: SnapshotTemporalScope | str | None = None,
    ) -> tuple[HeatmapSnapshot, ...]:
        """List exact-request snapshots oldest first, optionally by reuse scope."""

        fingerprint = _coerce_request_fingerprint(request)
        return self.list_for_requests((fingerprint,), temporal_scope=temporal_scope)[
            fingerprint
        ]

    def list_for_requests(
        self,
        requests: Iterable[HeatmapRequest | Mapping[str, Any] | str],
        *,
        temporal_scope: SnapshotTemporalScope | str | None = None,
    ) -> dict[str, tuple[HeatmapSnapshot, ...]]:
        """List exact-request snapshots for many requests in one cache scan.

        The persistent sidecar index is synchronized against the cheap set of
        main record IDs first. Legacy main records are read, integrity checked,
        and indexed once. Warm lookups then load large heatmap payloads only for
        the requested fingerprints, while every selected pointer and snapshot
        is checksum verified.
        """

        fingerprints = tuple(
            sorted({_coerce_request_fingerprint(request) for request in requests})
        )
        if not fingerprints:
            return {}
        scope = (
            SnapshotTemporalScope(temporal_scope)
            if temporal_scope is not None
            else None
        )
        locations, backfilled = self._synchronize_request_index()
        snapshots_by_fingerprint: dict[str, list[HeatmapSnapshot]] = {
            fingerprint: [] for fingerprint in fingerprints
        }
        for identifier, indexed_fingerprint in sorted(locations.items()):
            if indexed_fingerprint not in snapshots_by_fingerprint:
                continue
            pointer = self._get_index_pointer(indexed_fingerprint, identifier)
            snapshot = backfilled.get(identifier)
            if snapshot is None:
                snapshot = self._get_indexed_snapshot(pointer)
            self._validate_index_link(pointer, snapshot)
            if scope is not None and snapshot.temporal_scope is not scope:
                continue
            snapshots_by_fingerprint[snapshot.request_fingerprint].append(snapshot)

        return {
            fingerprint: tuple(
                sorted(
                    snapshots,
                    key=lambda item: (item.collected_at_utc, item.snapshot_id),
                )
            )
            for fingerprint, snapshots in snapshots_by_fingerprint.items()
        }

    def _synchronize_request_index(
        self,
    ) -> tuple[dict[str, str], dict[str, HeatmapSnapshot]]:
        # Read the index inventory before the main inventory. A concurrent
        # publisher always writes main first, so it can appear as an unindexed
        # record below and safely converge through the append-only write path.
        locations = self._indexed_snapshot_locations()
        main_ids = set(self._cache.record_ids())
        orphaned = sorted(set(locations) - main_ids)
        if orphaned:
            raise CacheCorruptionError(
                f"heatmap request index references a missing snapshot: {orphaned[0]}"
            )

        backfilled: dict[str, HeatmapSnapshot] = {}
        for identifier in sorted(main_ids - set(locations)):
            snapshot = self.get(identifier)
            if snapshot is None:
                raise CacheCorruptionError(
                    f"heatmap snapshot disappeared during index rebuild: {identifier}"
                )
            self._publish_index_pointer(
                snapshot,
                snapshot_path=self._cache.path_for(identifier),
            )
            locations[identifier] = snapshot.request_fingerprint
            backfilled[identifier] = snapshot
        return locations, backfilled

    def _indexed_snapshot_locations(self) -> dict[str, str]:
        if not self._request_index_root.exists():
            return {}
        locations: dict[str, str] = {}
        for path in self._request_index_root.rglob("*.json"):
            relative_parts = path.relative_to(self._request_index_root).parts
            try:
                if len(relative_parts) != 4:
                    raise ValueError("unexpected directory depth")
                fingerprint_prefix, fingerprint, snapshot_prefix, filename = (
                    relative_parts
                )
                identifier = Path(filename).stem
                _validate_identifier(fingerprint)
                _validate_identifier(identifier)
                if fingerprint_prefix != fingerprint[:2]:
                    raise ValueError("request fingerprint shard mismatch")
                if snapshot_prefix != identifier[:2]:
                    raise ValueError("snapshot ID shard mismatch")
                if identifier in locations:
                    raise ValueError("snapshot ID appears more than once")
            except ValueError as exc:
                raise CacheCorruptionError(
                    f"invalid heatmap request index path: {path}"
                ) from exc
            locations[identifier] = fingerprint
        return locations

    def _index_cache(self, request_fingerprint: str) -> JsonDiskCache:
        fingerprint = _validate_identifier(request_fingerprint)
        return JsonDiskCache(
            self._request_index_root / fingerprint[:2] / fingerprint,
            clock=self._clock,
        )

    def _publish_index_pointer(
        self,
        snapshot: HeatmapSnapshot,
        *,
        snapshot_path: Path,
    ) -> None:
        pointer = HeatmapSnapshotIndexPointer(
            snapshot_id=snapshot.snapshot_id,
            request_fingerprint=snapshot.request_fingerprint,
            snapshot_content_checksum_sha256=snapshot.content_checksum_sha256,
            snapshot_file_checksum_sha256=_file_sha256(snapshot_path),
        )
        index = self._index_cache(snapshot.request_fingerprint)
        try:
            index.add(snapshot.snapshot_id, pointer.model_dump(mode="json"))
        except FileExistsError:
            existing = self._get_index_pointer(
                snapshot.request_fingerprint, snapshot.snapshot_id
            )
            self._validate_index_link(existing, snapshot)

    def _get_index_pointer(
        self,
        request_fingerprint: str,
        snapshot_id: str,
    ) -> HeatmapSnapshotIndexPointer:
        payload = self._index_cache(request_fingerprint).get(snapshot_id)
        if payload is None:
            raise CacheCorruptionError(
                f"heatmap request index pointer is missing: {snapshot_id}"
            )
        try:
            pointer = HeatmapSnapshotIndexPointer.model_validate(payload)
        except ValidationError as exc:
            raise CacheCorruptionError(
                f"invalid heatmap request index pointer: {snapshot_id}"
            ) from exc
        if (
            pointer.snapshot_id != snapshot_id
            or pointer.request_fingerprint != request_fingerprint
        ):
            raise CacheCorruptionError(
                f"heatmap request index pointer has inconsistent keys: {snapshot_id}"
            )
        return pointer

    def _get_indexed_snapshot(
        self,
        pointer: HeatmapSnapshotIndexPointer,
    ) -> HeatmapSnapshot:
        """Load an indexed main file once, anchored by its sidecar byte hash."""

        path = self._cache.path_for(pointer.snapshot_id)
        try:
            encoded = path.read_bytes()
        except OSError as exc:
            raise CacheCorruptionError(
                f"indexed heatmap snapshot is missing: {pointer.snapshot_id}"
            ) from exc
        if hashlib.sha256(encoded).hexdigest() != pointer.snapshot_file_checksum_sha256:
            raise CacheCorruptionError(
                "indexed heatmap snapshot file checksum mismatch: "
                f"{pointer.snapshot_id}"
            )
        try:
            entry = json.loads(encoded)
            if not isinstance(entry, dict):
                raise ValueError("cache entry must be an object")
            if entry.get("cache_schema_version") != 3:
                raise ValueError("unsupported cache schema version")
            if entry.get("record_id") != pointer.snapshot_id:
                raise ValueError("stored record ID does not match its filename")
            payload = entry.get("payload")
            if not isinstance(payload, dict):
                raise ValueError("snapshot payload must be an object")
            if payload.get("snapshot_schema_version") != 1:
                raise ValueError("unsupported snapshot schema version")
            if payload.get("snapshot_id") != pointer.snapshot_id:
                raise ValueError("snapshot ID does not match its index pointer")
            if payload.get("request_fingerprint") != pointer.request_fingerprint:
                raise ValueError("request fingerprint does not match its index pointer")
            if payload.get("completion_status") != "completed":
                raise ValueError("indexed snapshot is not completed")
            content_checksum = payload.get("content_checksum_sha256")
            if content_checksum != pointer.snapshot_content_checksum_sha256:
                raise ValueError("snapshot checksum does not match its index pointer")
            activity_id = payload.get("activity_id")
            if not isinstance(activity_id, str) or not activity_id.strip():
                raise ValueError("snapshot activity ID is invalid")
            collected_at = require_utc_datetime(
                payload.get("collected_at_utc"), field_name="collected_at_utc"
            )
            temporal_scope = SnapshotTemporalScope(payload.get("temporal_scope"))
            request_contract = payload.get("request_contract")
            if not isinstance(request_contract, dict):
                raise ValueError("request contract must be an object")
            request = HeatmapRequest.model_validate(request_contract)
            if heatmap_request_fingerprint(request) != pointer.request_fingerprint:
                raise ValueError("indexed request contract fingerprint mismatch")
            raw_result = payload.get("raw_result")
            if not isinstance(raw_result, dict) or not raw_result:
                raise ValueError("raw result must be a non-empty object")
            expected_id = heatmap_snapshot_id(
                pointer.request_fingerprint,
                collected_at,
                activity_id,
            )
            if expected_id != pointer.snapshot_id:
                raise ValueError("snapshot immutable identity mismatch")
        except (TypeError, ValueError, ValidationError) as exc:
            raise CacheCorruptionError(
                f"invalid indexed heatmap snapshot: {pointer.snapshot_id}"
            ) from exc

        return HeatmapSnapshot.model_construct(
            snapshot_schema_version=1,
            snapshot_id=pointer.snapshot_id,
            request_fingerprint=pointer.request_fingerprint,
            temporal_scope=temporal_scope,
            completion_status="completed",
            activity_id=activity_id.strip(),
            collected_at_utc=collected_at,
            request_contract=request_contract,
            raw_result=raw_result,
            content_checksum_sha256=content_checksum,
        )

    @staticmethod
    def _validate_index_link(
        pointer: HeatmapSnapshotIndexPointer,
        snapshot: HeatmapSnapshot,
    ) -> None:
        if (
            pointer.snapshot_id != snapshot.snapshot_id
            or pointer.request_fingerprint != snapshot.request_fingerprint
            or pointer.snapshot_content_checksum_sha256
            != snapshot.content_checksum_sha256
        ):
            raise CacheCorruptionError(
                "heatmap request index pointer does not match its snapshot: "
                f"{snapshot.snapshot_id}"
            )

    def lookup_historical(
        self,
        request: HeatmapRequest | Mapping[str, Any] | str,
    ) -> HeatmapSnapshot | None:
        """Reuse the newest exact snapshot explicitly marked historical."""

        snapshots = self.list_for_request(
            request, temporal_scope=SnapshotTemporalScope.HISTORICAL
        )
        return snapshots[-1] if snapshots else None

    def lookup_current_or_forecast(
        self,
        request: HeatmapRequest | Mapping[str, Any] | str,
        *,
        ttl: timedelta,
        now_utc: datetime | None = None,
    ) -> HeatmapSnapshot | None:
        """Reuse the newest exact snapshot only while an explicit TTL is valid."""

        if not isinstance(ttl, timedelta) or ttl <= timedelta(0):
            raise ValueError("ttl must be an explicit positive timedelta")
        now = require_utc_datetime(
            now_utc if now_utc is not None else self._clock(),
            field_name="now_utc",
        )
        snapshots = self.list_for_request(
            request, temporal_scope=SnapshotTemporalScope.CURRENT_OR_FORECAST
        )
        for snapshot in reversed(snapshots):
            age = now - snapshot.collected_at_utc
            if timedelta(0) <= age <= ttl:
                return snapshot
        return None


def heatmap_snapshot_id(
    request_fingerprint: str,
    collected_at_utc: datetime,
    activity_id: str,
) -> str:
    """Return the deterministic identity of one collected result vintage."""

    _validate_identifier(request_fingerprint)
    collected = require_utc_datetime(collected_at_utc, field_name="collected_at_utc")
    normalized_activity_id = activity_id.strip()
    if not normalized_activity_id:
        raise ValueError("activity_id cannot be blank")
    return _json_sha256(
        {
            "activity_id": normalized_activity_id,
            "collected_at_utc": format_utc_datetime(collected),
            "record_kind": "fortyguard_heatmap_snapshot_v1",
            "request_fingerprint": request_fingerprint,
        }
    )


def _json_sha256(value: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise CacheCorruptionError(
            f"could not checksum heatmap snapshot file: {path.name}"
        ) from exc
    return digest.hexdigest()


def _coerce_request_fingerprint(
    request: HeatmapRequest | Mapping[str, Any] | str,
) -> str:
    if isinstance(request, str):
        return _validate_identifier(request)
    return heatmap_request_fingerprint(request)


def _validate_identifier(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("identifier must be 64 lowercase hex characters")
    return value
