"""Validate customer job manifests before planning or API collection.

The module deliberately has no Streamlit dependency.  Callers receive a small
result object containing either a normalized manifest or safe, user-facing
issues.  Raw cell values and validation exception text are never included in
those issues.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import time
from typing import Final

import pandas as pd
from pydantic import ValidationError

from certiroute.domain import Job

REQUIRED_JOB_COLUMNS: Final[tuple[str, ...]] = (
    "job_id",
    "name",
    "latitude",
    "longitude",
    "duration_minutes",
    "priority",
    "earliest_start",
    "latest_finish",
)
MIN_MANIFEST_JOBS: Final = 2
MAX_MANIFEST_JOBS: Final = 9

_TIME_PATTERN = re.compile(r"^\d{2}:\d{2}$")
_PYDANTIC_FIELD_MESSAGES: Final[dict[str, str]] = {
    "job_id": "must contain at least one character",
    "name": "must contain at least one character",
    "latitude": "must be between -90 and 90",
    "longitude": "must be between -180 and 180",
    "duration_minutes": "must be between 1 and 1440",
    "priority": "must be between 1 and 5",
    "earliest_start": "must be a valid 24-hour time (HH:MM)",
    "latest_finish": "must be a valid 24-hour time (HH:MM)",
}


@dataclass(frozen=True, slots=True)
class JobManifestIssue:
    """One safe validation issue addressed to a row and/or field."""

    message: str
    row_number: int | None = None
    field: str | None = None

    @property
    def display_message(self) -> str:
        """Return concise copy suitable for ``st.error`` or a validation list."""

        location: list[str] = []
        if self.row_number is not None:
            location.append(f"Row {self.row_number}")
        if self.field is not None:
            location.append(self.field)
        return f"{' · '.join(location)}: {self.message}" if location else self.message


@dataclass(frozen=True, slots=True)
class JobManifest:
    """A normalized, validated manifest ready for route planning."""

    frame: pd.DataFrame
    jobs: tuple[Job, ...]
    fingerprint: str


@dataclass(frozen=True, slots=True)
class JobManifestValidation:
    """Success-or-issues result that avoids exceptions in an interactive UI."""

    manifest: JobManifest | None
    issues: tuple[JobManifestIssue, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.manifest is not None and not self.issues

    @property
    def error_messages(self) -> tuple[str, ...]:
        return tuple(issue.display_message for issue in self.issues)


def validate_job_manifest(frame: pd.DataFrame) -> JobManifestValidation:
    """Normalize and validate a customer-supplied work-order table.

    Extra columns are ignored.  Required columns are returned in the canonical
    order above, and row order is preserved because it represents the
    customer's current operational sequence.
    """

    if not isinstance(frame, pd.DataFrame):
        return _invalid(JobManifestIssue("Upload must be a tabular CSV file."))

    issues: list[JobManifestIssue] = []
    row_count = len(frame)
    if not MIN_MANIFEST_JOBS <= row_count <= MAX_MANIFEST_JOBS:
        issues.append(
            JobManifestIssue(
                f"Provide between {MIN_MANIFEST_JOBS} and {MAX_MANIFEST_JOBS} jobs."
            )
        )

    column_counts = {
        column: sum(existing == column for existing in frame.columns)
        for column in REQUIRED_JOB_COLUMNS
    }
    missing = [column for column, count in column_counts.items() if count == 0]
    repeated = [column for column, count in column_counts.items() if count > 1]
    if missing:
        issues.append(
            JobManifestIssue(
                "Missing required columns: " + ", ".join(missing) + ".",
                field="columns",
            )
        )
    if repeated:
        issues.append(
            JobManifestIssue(
                "Each required column must appear exactly once.",
                field="columns",
            )
        )
    if missing or repeated:
        return JobManifestValidation(manifest=None, issues=tuple(issues))

    source = frame.loc[:, list(REQUIRED_JOB_COLUMNS)]
    normalized_rows: list[dict[str, object]] = []
    jobs: list[Job] = []
    normalized_ids: list[str | None] = []

    for position, values in enumerate(source.itertuples(index=False, name=None), 1):
        raw = dict(zip(REQUIRED_JOB_COLUMNS, values, strict=True))
        row_issues: list[JobManifestIssue] = []

        for field in REQUIRED_JOB_COLUMNS:
            if _is_null(raw[field]):
                row_issues.append(
                    JobManifestIssue(
                        "is required",
                        row_number=position,
                        field=field,
                    )
                )

        job_id = _normalize_text(raw["job_id"], position, "job_id", row_issues)
        name = _normalize_text(raw["name"], position, "name", row_issues)
        latitude = _normalize_number(raw["latitude"], position, "latitude", row_issues)
        longitude = _normalize_number(
            raw["longitude"], position, "longitude", row_issues
        )
        duration = _normalize_integer(
            raw["duration_minutes"], position, "duration_minutes", row_issues
        )
        priority = _normalize_integer(raw["priority"], position, "priority", row_issues)
        earliest = _normalize_time(
            raw["earliest_start"], position, "earliest_start", row_issues
        )
        latest = _normalize_time(
            raw["latest_finish"], position, "latest_finish", row_issues
        )
        normalized_ids.append(job_id)

        parsed = (
            job_id,
            name,
            latitude,
            longitude,
            duration,
            priority,
            earliest,
            latest,
        )
        if all(value is not None for value in parsed):
            assert job_id is not None
            assert name is not None
            assert latitude is not None
            assert longitude is not None
            assert duration is not None
            assert priority is not None
            assert earliest is not None
            assert latest is not None
            try:
                job = Job(
                    job_id=job_id,
                    name=name,
                    location={"latitude": latitude, "longitude": longitude},
                    duration_minutes=duration,
                    priority=priority,
                    earliest_start=earliest,
                    latest_finish=latest,
                )
            except ValidationError as exc:
                row_issues.extend(_pydantic_issues(exc, row_number=position))
            else:
                earliest_minute = earliest.hour * 60 + earliest.minute
                latest_minute = latest.hour * 60 + latest.minute
                if earliest_minute >= latest_minute:
                    row_issues.append(
                        JobManifestIssue(
                            "must be earlier than latest_finish",
                            row_number=position,
                            field="earliest_start",
                        )
                    )
                elif duration > latest_minute - earliest_minute:
                    row_issues.append(
                        JobManifestIssue(
                            "must fit inside the job's time window",
                            row_number=position,
                            field="duration_minutes",
                        )
                    )

                if not row_issues:
                    jobs.append(job)
                    normalized_rows.append(_normalized_row(job))

        issues.extend(_deduplicate_issues(row_issues))

    duplicate_positions = _duplicate_id_positions(normalized_ids)
    for row_number in duplicate_positions:
        issues.append(
            JobManifestIssue(
                "must be unique; this ID appears more than once",
                row_number=row_number,
                field="job_id",
            )
        )

    if issues:
        return JobManifestValidation(manifest=None, issues=tuple(issues))

    normalized = pd.DataFrame(normalized_rows, columns=REQUIRED_JOB_COLUMNS)
    return JobManifestValidation(
        manifest=JobManifest(
            frame=normalized,
            jobs=tuple(jobs),
            fingerprint=_manifest_fingerprint(normalized_rows),
        )
    )


def _invalid(issue: JobManifestIssue) -> JobManifestValidation:
    return JobManifestValidation(manifest=None, issues=(issue,))


def _is_null(value: object) -> bool:
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return isinstance(result, bool) and result


def _normalize_text(
    value: object,
    row_number: int,
    field: str,
    issues: list[JobManifestIssue],
) -> str | None:
    if _is_null(value):
        return None
    if not isinstance(value, str):
        issues.append(
            JobManifestIssue(
                "must be text",
                row_number=row_number,
                field=field,
            )
        )
        return None
    normalized = unicodedata.normalize("NFC", value).strip()
    if not normalized:
        issues.append(
            JobManifestIssue(
                "cannot be blank",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return normalized


def _normalize_number(
    value: object,
    row_number: int,
    field: str,
    issues: list[JobManifestIssue],
) -> float | None:
    if _is_null(value):
        return None
    if isinstance(value, bool):
        number = None
    else:
        try:
            number = float(value)  # type: ignore[arg-type]
        except (TypeError, ValueError, OverflowError):
            number = None
    if number is None or not math.isfinite(number):
        issues.append(
            JobManifestIssue(
                "must be a finite number",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return 0.0 if number == 0 else number


def _normalize_integer(
    value: object,
    row_number: int,
    field: str,
    issues: list[JobManifestIssue],
) -> int | None:
    number = _normalize_number(value, row_number, field, issues)
    if number is None:
        return None
    if not number.is_integer():
        issues.append(
            JobManifestIssue(
                "must be a whole number",
                row_number=row_number,
                field=field,
            )
        )
        return None
    return int(number)


def _normalize_time(
    value: object,
    row_number: int,
    field: str,
    issues: list[JobManifestIssue],
) -> time | None:
    if _is_null(value):
        return None
    parsed: time | None = None
    if isinstance(value, time):
        if value.tzinfo is None and not value.second and not value.microsecond:
            parsed = value
    elif isinstance(value, str):
        candidate = value.strip()
        if _TIME_PATTERN.fullmatch(candidate):
            try:
                parsed = time.fromisoformat(candidate)
            except ValueError:
                parsed = None
    if parsed is None:
        issues.append(
            JobManifestIssue(
                "must be a valid 24-hour time (HH:MM)",
                row_number=row_number,
                field=field,
            )
        )
    return parsed


def _pydantic_issues(
    error: ValidationError, *, row_number: int
) -> list[JobManifestIssue]:
    issues: list[JobManifestIssue] = []
    for detail in error.errors(
        include_url=False, include_context=False, include_input=False
    ):
        location = tuple(str(part) for part in detail["loc"])
        field = location[-1] if location else None
        safe_field = field if field in REQUIRED_JOB_COLUMNS else None
        issues.append(
            JobManifestIssue(
                _PYDANTIC_FIELD_MESSAGES.get(safe_field or "", "is invalid"),
                row_number=row_number,
                field=safe_field,
            )
        )
    return issues


def _deduplicate_issues(
    issues: list[JobManifestIssue],
) -> tuple[JobManifestIssue, ...]:
    seen: set[tuple[int | None, str | None, str]] = set()
    unique: list[JobManifestIssue] = []
    for issue in issues:
        key = (issue.row_number, issue.field, issue.message)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return tuple(unique)


def _duplicate_id_positions(values: list[str | None]) -> tuple[int, ...]:
    counts: dict[str, int] = {}
    for value in values:
        if value is not None:
            counts[value] = counts.get(value, 0) + 1
    return tuple(
        position
        for position, value in enumerate(values, 1)
        if value is not None and counts[value] > 1
    )


def _normalized_row(job: Job) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "name": job.name,
        "latitude": 0.0 if job.location.latitude == 0 else job.location.latitude,
        "longitude": 0.0 if job.location.longitude == 0 else job.location.longitude,
        "duration_minutes": job.duration_minutes,
        "priority": job.priority,
        "earliest_start": job.earliest_start.strftime("%H:%M"),
        "latest_finish": job.latest_finish.strftime("%H:%M"),
    }


def _manifest_fingerprint(rows: list[dict[str, object]]) -> str:
    payload = {
        "schema": "certiroute-job-manifest-v1",
        "columns": list(REQUIRED_JOB_COLUMNS),
        "rows": [[row[column] for column in REQUIRED_JOB_COLUMNS] for row in rows],
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "MAX_MANIFEST_JOBS",
    "MIN_MANIFEST_JOBS",
    "REQUIRED_JOB_COLUMNS",
    "JobManifest",
    "JobManifestIssue",
    "JobManifestValidation",
    "validate_job_manifest",
]
