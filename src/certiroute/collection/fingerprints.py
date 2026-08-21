"""Canonical request fingerprints for the FortyGuard cache boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
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


def coerce_heatmap_request(
    request: HeatmapRequest | Mapping[str, Any],
) -> HeatmapRequest:
    """Validate mapping inputs at the same schema boundary as the API client."""

    if isinstance(request, HeatmapRequest):
        return request
    return HeatmapRequest.model_validate(request)
