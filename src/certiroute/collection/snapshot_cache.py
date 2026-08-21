"""Memoizing, append-only cache for completed FortyGuard heatmap snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
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

    @property
    def root(self) -> Path:
        """Resolved storage root; the default is under Git-ignored ``data/raw``."""

        return self._cache.root

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
        self._cache.add(identifier, snapshot.model_dump(mode="json"))
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
        scope = (
            SnapshotTemporalScope(temporal_scope)
            if temporal_scope is not None
            else None
        )
        snapshots = [
            snapshot
            for identifier in self._cache.record_ids()
            if (snapshot := self.get(identifier)) is not None
            and snapshot.request_fingerprint == fingerprint
            and (scope is None or snapshot.temporal_scope is scope)
        ]
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (item.collected_at_utc, item.snapshot_id),
            )
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
