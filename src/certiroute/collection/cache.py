"""Append-only, secret-aware JSON storage with atomic publication."""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from certiroute.collection._json import normalize_json_object

_IDENTIFIER_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CACHE_SCHEMA_VERSION = 3


class CacheCorruptionError(RuntimeError):
    """Raised when a stored entry cannot be trusted as complete and consistent."""


class JsonDiskCache:
    """Publish immutable structured JSON records by deterministic identifier.

    A fully flushed temporary file is hard-linked into its final name. Creating
    that link is atomic and fails if the identifier already exists, so concurrent
    writers cannot silently replace one another. The temporary file is always on
    the same filesystem as the final entry.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.root = Path(root).expanduser().resolve()
        self._clock = clock or (lambda: datetime.now(UTC))

    def get(self, record_id: str) -> dict[str, Any] | None:
        """Return a detached payload, or ``None`` when the identifier is absent."""

        path = self.path_for(record_id)
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
            if normalized.get("record_id") != record_id:
                raise ValueError("stored record ID does not match its filename")
            payload = normalize_json_object(normalized.get("payload"), path="$.payload")
            expected_digest = _payload_sha256(payload)
            if normalized.get("payload_sha256") != expected_digest:
                raise ValueError("payload checksum mismatch")
        except (TypeError, ValueError) as exc:
            raise CacheCorruptionError(
                f"invalid cache entry {path.name}: {exc}"
            ) from exc
        return payload

    def add(self, record_id: str, payload: Mapping[str, Any]) -> Path:
        """Atomically append one record; an existing identifier is never replaced."""

        path = self.path_for(record_id)
        safe_payload = normalize_json_object(payload, path="$.payload")
        now = _require_utc(self._clock(), field_name="cache clock")
        entry = {
            "cache_schema_version": _CACHE_SCHEMA_VERSION,
            "record_id": record_id,
            "stored_at_utc": _format_utc(now),
            "payload": safe_payload,
            "payload_sha256": _payload_sha256(safe_payload),
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
                prefix=f".{record_id}.",
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
            try:
                os.link(temporary_path, path)
            except FileExistsError as exc:
                raise FileExistsError(f"record already exists: {record_id}") from exc
            temporary_path.unlink()
            temporary_path = None
            _sync_directory(path.parent)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return path

    def record_ids(self) -> tuple[str, ...]:
        """List immutable record IDs in deterministic order."""

        if not self.root.exists():
            return ()
        identifiers: list[str] = []
        for path in self.root.glob("*/*.json"):
            if _IDENTIFIER_PATTERN.fullmatch(path.stem):
                identifiers.append(path.stem)
        return tuple(sorted(identifiers))

    def path_for(self, record_id: str) -> Path:
        """Resolve a traversal-safe, sharded filename for a record identifier."""

        if not _IDENTIFIER_PATTERN.fullmatch(record_id):
            raise ValueError("record_id must be 64 lowercase hex characters")
        return self.root / record_id[:2] / f"{record_id}.json"


def _require_utc(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(UTC)


def _format_utc(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _payload_sha256(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(canonical).hexdigest()


def _restrict_directory_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o700)
    except OSError:
        # Some ACL-backed filesystems do not implement POSIX modes.
        pass


def _restrict_file_permissions(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        # The payload boundary still rejects credential-like fields.
        pass


def _sync_directory(path: Path) -> None:
    if os.name == "nt" or not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
