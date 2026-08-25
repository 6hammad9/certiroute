"""What the landing page shows before anyone has done anything.

A landing page that only describes a product asks to be believed. This one
plays a real day instead: one the model had never trained or calibrated on,
resolved once by scripts/build_showcase.py and committed, so it renders with
no API call and no dependence on a snapshot cache that is not in the
repository.

The headline figures beside it come from the graded evidence files rather than
from prose, so they cannot drift away from what was actually measured.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_SHOWCASE_PATH = Path("data/showcase/graded_day.json")
DEFAULT_EVIDENCE_ROOT = Path("data/evidence")


@dataclass(frozen=True)
class GradedDay:
    """One day the model planned blind, and what it chose."""

    area_label: str
    day: date
    measured_level_c: float
    recommended_start: str
    baseline_start: str
    exposure_reduction: float | None
    site_count: int
    payload: dict[str, Any]

    @property
    def home_earlier_minutes(self) -> int | None:
        """How much sooner the recommended crew got back to base."""

        runs = self.payload.get("runs", [])
        if len(runs) < 2:
            return None
        return int(runs[1]["finish"] - runs[0]["finish"])


@dataclass(frozen=True)
class GradeSummary:
    """The whole graded record, added up across areas."""

    areas: tuple[str, ...]
    graded_days: int
    picked_best_start: int
    worst_regret_units: float
    best_reduction: float | None
    lowest_reduction: float | None
    mean_absolute_error_c: float | None
    # The same record for plans made the evening before. Counted separately,
    # because adding them to the same-day tally would double every figure
    # while describing the same nine days.
    day_ahead_days: int = 0
    day_ahead_best_start: int = 0

    @property
    def chose_best_every_time(self) -> bool:
        return self.graded_days > 0 and self.picked_best_start == self.graded_days


def load_graded_day(path: Path | None = None) -> GradedDay | None:
    """Load the committed showcase, or None when it has not been built."""

    source = path if path is not None else DEFAULT_SHOWCASE_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    detail = payload.get("showcase")
    if not isinstance(detail, dict) or not payload.get("runs"):
        return None
    return GradedDay(
        area_label=str(detail["label"]),
        day=date.fromisoformat(detail["date"]),
        measured_level_c=float(detail["measured_level_c"]),
        recommended_start=str(detail["recommended_start"]),
        baseline_start=str(detail["baseline_start"]),
        exposure_reduction=(
            None
            if detail.get("exposure_reduction") is None
            else float(detail["exposure_reduction"])
        ),
        site_count=int(detail["site_count"]),
        payload=payload,
    )


def load_grade_summary(root: Path | None = None) -> GradeSummary | None:
    """Add up every committed grade file into one honest headline."""

    base = root if root is not None else DEFAULT_EVIDENCE_ROOT
    if not base.is_dir():
        return None

    areas: list[str] = []
    days = 0
    best = 0
    ahead_days = 0
    ahead_best = 0
    regrets: list[float] = []
    reductions: list[float] = []
    errors: list[float] = []
    for path in sorted(base.glob("recommendation_grades_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            summary = payload["summary"]
        except (json.JSONDecodeError, KeyError):
            continue
        if payload.get("planned_the_evening_before"):
            ahead_days += int(summary["graded_day_count"])
            ahead_best += int(summary["picked_best_start"])
            continue
        areas.append(str(payload.get("area_id", path.stem)))
        days += int(summary["graded_day_count"])
        best += int(summary["picked_best_start"])
        regrets.append(float(summary["worst_regret_units"]))
        reductions.extend(
            float(entry["realized_reduction"])
            for entry in payload.get("graded_days", [])
            if entry.get("realized_reduction") is not None
        )
        error = payload.get("model", {}).get("held_out_mae_c")
        if error is not None:
            errors.append(float(error))

    if not areas:
        return None
    return GradeSummary(
        areas=tuple(areas),
        graded_days=days,
        picked_best_start=best,
        worst_regret_units=max(regrets) if regrets else 0.0,
        best_reduction=max(reductions) if reductions else None,
        lowest_reduction=min(reductions) if reductions else None,
        mean_absolute_error_c=(sum(errors) / len(errors) if errors else None),
        day_ahead_days=ahead_days,
        day_ahead_best_start=ahead_best,
    )


__all__ = [
    "DEFAULT_EVIDENCE_ROOT",
    "DEFAULT_SHOWCASE_PATH",
    "GradeSummary",
    "GradedDay",
    "load_grade_summary",
    "load_graded_day",
]
