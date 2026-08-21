"""Small, secret-aware JSON disk cache with atomic file replacement."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from certiroute.collection._json import normalize_json_object

_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CACHE_SCHEMA_VERSION = 1


class CacheCorruptionError(RuntimeError):
    """Raised when a cache file cannot be trusted as a complete cache entry."""


class JsonDiskCache:
    """Store one structured JSON payload per normalized request fingerprint.

    Values are written to a temporary file in the destination directory, flushed
    to disk, and atomically installed with ``os.replace``. Headers, environment
    values, and API clients are never accepted by this interface; secret-like
    JSON field names are rejected recursively as a second line of defense.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, request_fingerprint: str) -> dict[str, Any] | None:
        """Return a detached payload, or ``None`` for a cache miss."""

        path = self.path_for(request_fingerprint)
        if not path.exists():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                entry = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CacheCorruptionError(
                f"could not read cache entry {path.name}"
            ) from exc

        try:
            normalized = normalize_json_object(entry)
            if normalized.get("cache_schema_version") != _CACHE_SCHEMA_VERSION:
                raise ValueError("unsupported cache schema version")
            if normalized.get("request_fingerprint") != request_fingerprint:
                raise ValueError("cache fingerprint does not match its filename")
            payload = normalize_json_object(normalized.get("payload"), path="$.payload")
        except (TypeError, ValueError) as exc:
            raise CacheCorruptionError(
                f"invalid cache entry {path.name}: {exc}"
            ) from exc
        return payload

    def put(
        self,
        request_fingerprint: str,
        payload: Mapping[str, Any],
        *,
        overwrite: bool = True,
    ) -> Path:
        """Atomically persist a structured payload and return its final path."""

        path = self.path_for(request_fingerprint)
        if not overwrite and path.exists():
            raise FileExistsError(f"cache entry already exists: {request_fingerprint}")

        safe_payload = normalize_json_object(payload, path="$.payload")
        now = _require_utc(self._clock(), field_name="cache clock")
        entry = {
            "cache_schema_version": _CACHE_SCHEMA_VERSION,
            "request_fingerprint": request_fingerprint,
            "stored_at_utc": _format_utc(now),
            "payload": safe_payload,
        }

        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_directory_permissions(self.root)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        _restrict_directory_permissions(path.parent)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{request_fingerprint}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                json.dump(
                    entry,
                    handle,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _restrict_file_permissions(temporary_path)
            os.replace(temporary_path, path)
            temporary_path = None
            _sync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def path_for(self, request_fingerprint: str) -> Path:
        """Resolve a traversal-safe, sharded filename for a request fingerprint."""

        if not _FINGERPRINT_PATTERN.fullmatch(request_fingerprint):
            raise ValueError("request_fingerprint must be 64 lowercase hex characters")
        return self.root / request_fingerprint[:2] / f"{request_fingerprint}.json"


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _restrict_directory_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        # ACL-backed filesystems (notably some Windows setups) may not implement
        # POSIX modes. Atomic replacement and secret screening still apply.
        pass


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # See the directory-permission note above. The cache never persists
        # credentials even when the filesystem does not expose POSIX modes.
        pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
