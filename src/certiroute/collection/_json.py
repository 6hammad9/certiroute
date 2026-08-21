"""Strict JSON normalization and secret-key guards for persisted data."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any


class UnsafeCachePayloadError(ValueError):
    """Raised when data proposed for persistence contains a secret-like field."""


_SENSITIVE_KEYS = {
    "apikey",
    "xapikey",
    "authorization",
    "proxyauthorization",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "clientsecret",
    "secret",
    "password",
    "passwd",
    "cookie",
    "setcookie",
}
_SENSITIVE_SUFFIXES = (
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "password",
)


def normalize_json(
    value: Any,
    *,
    reject_sensitive_keys: bool = True,
    path: str = "$",
) -> Any:
    """Return a detached JSON value or fail instead of serializing ambiguously.

    The persistence boundary deliberately accepts only actual JSON values. In
    particular, it rejects NaN/infinity and objects whose string conversion
    could accidentally disclose credentials.
    """

    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} contains a non-finite number")
        # Treat negative zero as ordinary zero for stable fingerprints.
        return 0.0 if value == 0 else value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} contains a non-string object key")
            if reject_sensitive_keys and _looks_sensitive(key):
                raise UnsafeCachePayloadError(
                    f"refusing to persist secret-like field at {path}.{key}"
                )
            normalized[key] = normalize_json(
                item,
                reject_sensitive_keys=reject_sensitive_keys,
                path=f"{path}.{key}",
            )
        return normalized
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [
            normalize_json(
                item,
                reject_sensitive_keys=reject_sensitive_keys,
                path=f"{path}[{index}]",
            )
            for index, item in enumerate(value)
        ]
    raise TypeError(f"{path} contains non-JSON value of type {type(value).__name__}")


def normalize_json_object(
    value: Any, *, reject_sensitive_keys: bool = True, path: str = "$"
) -> dict[str, Any]:
    """Normalize a JSON object and reject scalar or array roots."""

    normalized = normalize_json(
        value,
        reject_sensitive_keys=reject_sensitive_keys,
        path=path,
    )
    if not isinstance(normalized, dict):
        raise TypeError(f"{path} must be a JSON object")
    return normalized


def _looks_sensitive(key: str) -> bool:
    compact = re.sub(r"[^a-z0-9]", "", key.lower())
    return compact in _SENSITIVE_KEYS or compact.endswith(_SENSITIVE_SUFFIXES)
