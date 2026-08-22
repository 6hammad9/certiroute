from __future__ import annotations

from datetime import time

import pandas as pd
import pytest

from certiroute.job_manifest import (
    MAX_MANIFEST_JOBS,
    MIN_MANIFEST_JOBS,
    REQUIRED_JOB_COLUMNS,
    validate_job_manifest,
)


def valid_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "job_id": ["001", "002"],
            "name": ["Cabinet inspection", "Pump service"],
            "latitude": ["33.44855", "33.44965"],
            "longitude": ["-112.07391", "-112.04760"],
            "duration_minutes": ["55", "65"],
            "priority": ["5", "4"],
            "earliest_start": ["08:00", "08:30"],
            "latest_finish": ["12:00", "16:00"],
        }
    )


def issue_for(result, *, row: int | None = None, field: str | None = None):
    return [
        issue
        for issue in result.issues
        if (row is None or issue.row_number == row)
        and (field is None or issue.field == field)
    ]


def test_valid_manifest_is_normalized_for_planning() -> None:
    frame = valid_frame()
    frame["unused_customer_note"] = ["ignored", "ignored"]
    frame = frame[
        [
            "name",
            "unused_customer_note",
            *[c for c in REQUIRED_JOB_COLUMNS if c != "name"],
        ]
    ]
    frame.loc[0, "job_id"] = " 001 "
    frame.loc[0, "name"] = " Cabinet inspection "

    result = validate_job_manifest(frame)

    assert result.is_valid
    assert result.issues == ()
    assert result.error_messages == ()
    assert result.manifest is not None
    assert tuple(result.manifest.frame.columns) == REQUIRED_JOB_COLUMNS
    assert result.manifest.frame["job_id"].tolist() == ["001", "002"]
    assert result.manifest.frame["name"].tolist()[0] == "Cabinet inspection"
    assert result.manifest.frame["latitude"].tolist() == [33.44855, 33.44965]
    assert result.manifest.frame["duration_minutes"].tolist() == [55, 65]
    assert result.manifest.frame["earliest_start"].tolist() == ["08:00", "08:30"]
    assert [job.job_id for job in result.manifest.jobs] == ["001", "002"]
    assert len(result.manifest.fingerprint) == 64
    int(result.manifest.fingerprint, 16)


def test_fingerprint_is_stable_for_equivalent_normalized_manifests() -> None:
    first = valid_frame()
    second = valid_frame().copy()
    second["latitude"] = second["latitude"].astype(float)
    second["longitude"] = second["longitude"].astype(float)
    second["duration_minutes"] = second["duration_minutes"].astype(int)
    second["priority"] = second["priority"].astype(int)
    second["earliest_start"] = [time(8), time(8, 30)]
    second["latest_finish"] = [time(12), time(16)]
    second["name"] = second["name"].map(lambda value: f"  {value}  ")
    second = second[list(reversed(REQUIRED_JOB_COLUMNS))]

    first_result = validate_job_manifest(first)
    second_result = validate_job_manifest(second)

    assert first_result.manifest is not None
    assert second_result.manifest is not None
    assert first_result.manifest.fingerprint == second_result.manifest.fingerprint


def test_fingerprint_covers_values_and_operational_row_order() -> None:
    original = validate_job_manifest(valid_frame())
    changed_frame = valid_frame()
    changed_frame.loc[0, "name"] = "Different task"
    changed = validate_job_manifest(changed_frame)
    reordered = validate_job_manifest(valid_frame().iloc[::-1].reset_index(drop=True))

    assert original.manifest is not None
    assert changed.manifest is not None
    assert reordered.manifest is not None
    assert original.manifest.fingerprint != changed.manifest.fingerprint
    assert original.manifest.fingerprint != reordered.manifest.fingerprint


def test_missing_and_repeated_required_columns_are_rejected() -> None:
    missing = validate_job_manifest(valid_frame().drop(columns="priority"))
    duplicated_frame = valid_frame()
    duplicated_frame.columns = [
        *REQUIRED_JOB_COLUMNS[:-1],
        "earliest_start",
    ]
    repeated = validate_job_manifest(duplicated_frame)

    assert not missing.is_valid
    assert missing.manifest is None
    assert issue_for(missing, field="columns")
    assert "priority" in missing.error_messages[0]
    assert not repeated.is_valid
    assert any("exactly once" in message for message in repeated.error_messages)


@pytest.mark.parametrize("null_value", [None, float("nan"), pd.NA])
def test_null_cells_are_rejected_without_secondary_value_errors(null_value) -> None:
    frame = valid_frame()
    frame.loc[0, "latitude"] = null_value

    result = validate_job_manifest(frame)

    latitude_issues = issue_for(result, row=1, field="latitude")
    assert [issue.message for issue in latitude_issues] == ["is required"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("latitude", "not-a-coordinate"),
        ("longitude", True),
        ("duration_minutes", "many"),
        ("priority", "urgent"),
    ],
)
def test_non_numeric_values_are_rejected_with_safe_field_errors(field, value) -> None:
    frame = valid_frame()
    frame.loc[0, field] = value

    result = validate_job_manifest(frame)

    errors = issue_for(result, row=1, field=field)
    assert errors
    assert errors[0].message == "must be a finite number"
    assert str(value) not in " ".join(result.error_messages)


@pytest.mark.parametrize("field", ["duration_minutes", "priority"])
def test_integer_fields_reject_fractional_values(field: str) -> None:
    frame = valid_frame()
    frame.loc[0, field] = "2.5"

    result = validate_job_manifest(frame)

    assert any(
        issue.message == "must be a whole number"
        for issue in issue_for(result, row=1, field=field)
    )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("latitude", 90.1, "must be between -90 and 90"),
        ("longitude", -180.1, "must be between -180 and 180"),
        ("duration_minutes", 0, "must be between 1 and 1440"),
        ("duration_minutes", 1441, "must be between 1 and 1440"),
        ("priority", 0, "must be between 1 and 5"),
        ("priority", 6, "must be between 1 and 5"),
    ],
)
def test_out_of_range_domain_fields_are_reported_safely(field, value, message) -> None:
    frame = valid_frame()
    frame.loc[0, field] = value

    result = validate_job_manifest(frame)

    assert any(
        issue.message == message for issue in issue_for(result, row=1, field=field)
    )


def test_duplicate_ids_are_detected_after_whitespace_normalization() -> None:
    frame = valid_frame()
    frame.loc[0, "job_id"] = " private-id "
    frame.loc[1, "job_id"] = "private-id"

    result = validate_job_manifest(frame)

    assert [issue.row_number for issue in issue_for(result, field="job_id")] == [1, 2]
    assert "private-id" not in " ".join(result.error_messages)


@pytest.mark.parametrize("value", ["8am", "08:00:30", "25:00"])
def test_times_must_be_valid_hh_mm_values(value: str) -> None:
    frame = valid_frame()
    frame.loc[0, "earliest_start"] = value

    result = validate_job_manifest(frame)

    assert any(
        "HH:MM" in issue.message
        for issue in issue_for(result, row=1, field="earliest_start")
    )
    assert value not in " ".join(result.error_messages)


@pytest.mark.parametrize(
    ("earliest", "latest"),
    [("12:00", "12:00"), ("13:00", "12:00")],
)
def test_earliest_start_must_precede_latest_finish(earliest: str, latest: str) -> None:
    frame = valid_frame()
    frame.loc[0, "earliest_start"] = earliest
    frame.loc[0, "latest_finish"] = latest

    result = validate_job_manifest(frame)

    assert any(
        issue.message == "must be earlier than latest_finish"
        for issue in issue_for(result, row=1, field="earliest_start")
    )


def test_duration_must_fit_inside_time_window() -> None:
    frame = valid_frame()
    frame.loc[0, "earliest_start"] = "08:00"
    frame.loc[0, "latest_finish"] = "08:30"
    frame.loc[0, "duration_minutes"] = 31

    result = validate_job_manifest(frame)

    assert any(
        issue.message == "must fit inside the job's time window"
        for issue in issue_for(result, row=1, field="duration_minutes")
    )


@pytest.mark.parametrize("row_count", [MIN_MANIFEST_JOBS - 1, MAX_MANIFEST_JOBS + 1])
def test_manifest_job_count_is_bounded(row_count: int) -> None:
    source = valid_frame().iloc[[0]]
    frame = pd.concat([source] * row_count, ignore_index=True)
    frame["job_id"] = [f"J-{index}" for index in range(row_count)]

    result = validate_job_manifest(frame)

    assert not result.is_valid
    assert any(
        f"between {MIN_MANIFEST_JOBS} and {MAX_MANIFEST_JOBS}" in message
        for message in result.error_messages
    )


def test_errors_use_safe_row_positions_not_dataframe_indices_or_cell_values() -> None:
    frame = valid_frame()
    frame.index = ["private-customer-key", "other-private-key"]
    frame.loc["private-customer-key", "latitude"] = "secret-value"

    result = validate_job_manifest(frame)
    messages = " ".join(result.error_messages)

    assert "Row 1" in messages
    assert "latitude" in messages
    assert "private-customer-key" not in messages
    assert "secret-value" not in messages


def test_non_dataframe_input_returns_a_validation_result() -> None:
    result = validate_job_manifest(None)  # type: ignore[arg-type]

    assert not result.is_valid
    assert result.manifest is None
    assert result.error_messages == ("Upload must be a tabular CSV file.",)
