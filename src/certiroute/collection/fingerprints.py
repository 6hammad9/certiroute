"""Canonical request fingerprints for the FortyGuard cache boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from certiroute.collection._json import normalize_json_object
from certiroute.fortyguard.schemas import HeatmapRequest


def normalize_heatmap_request(
    request: HeatmapRequest | Mapping[str, Any],
) -> dict[str, Any]:
    """Return the validated API request as deterministic JSON-compatible data.

    Object key order is intentionally irrelevant, while array order remains
    significant because feature and polygon-ring order are part of the request.
    """

    validated = coerce_heatmap_request(request)
    payload = validated.model_dump(mode="json", exclude_none=True)
    # This value is only canonicalized in memory and hashed. It is not written by
    # the archive, which persists an AOI with feature properties removed.
    return normalize_json_object(payload, reject_sensitive_keys=False)


def canonical_heatmap_request_json(
    request: HeatmapRequest | Mapping[str, Any],
) -> str:
    """Serialize a heatmap request with stable key ordering and separators."""

    return json.dumps(
        normalize_heatmap_request(request),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def heatmap_request_fingerprint(
    request: HeatmapRequest | Mapping[str, Any],
) -> str:
    """Return a lowercase SHA-256 fingerprint of a normalized request."""

    canonical = canonical_heatmap_request_json(request).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def forecast_record_id(
    request_fingerprint: str,
    requested_at_utc: datetime,
    activity_id: str,
) -> str:
    """Identify one immutable issuance vintage of a normalized request."""

    identity = {
        "activity_id": _normalized_activity_id(activity_id),
        "record_kind": "forecast_vintage_v1",
        "request_fingerprint": _validated_identifier(request_fingerprint),
        "requested_at_utc": _format_utc(requested_at_utc),
    }
    return _identity_hash(identity)


def realization_record_id(
    forecast_id: str,
    recorded_at_utc: datetime,
    activity_id: str,
) -> str:
    """Identify one immutable later-vendor realization vintage."""

    identity = {
        "activity_id": _normalized_activity_id(activity_id),
        "forecast_record_id": _validated_identifier(forecast_id),
        "record_kind": "vendor_relative_realization_v1",
        "recorded_at_utc": _format_utc(recorded_at_utc),
    }
    return _identity_hash(identity)


def coerce_heatmap_request(
    request: HeatmapRequest | Mapping[str, Any],
) -> HeatmapRequest:
    """Validate mapping inputs at the same schema boundary as the API client."""

    if isinstance(request, HeatmapRequest):
        return request
    return HeatmapRequest.model_validate(request)


def _identity_hash(identity: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        identity,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _format_utc(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("record identity timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _normalized_activity_id(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("activity_id cannot be blank")
    return normalized


def _validated_identifier(value: str) -> str:
    if len(value) != 64 or any(
        character not in "0123456789abcdef" for character in value
    ):
        raise ValueError("record identifiers must be 64 lowercase hex characters")
    return value
