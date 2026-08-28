"""Measured hourly temperatures, distilled to the sites they were taken for.

The snapshot cache holds whole heatmaps - 6847 tiles for a single hour of one
area - which comes to 1.8 GB and cannot travel with the repository. Almost all
of it is tiles nobody asked about: what the product ever reads is the
temperature at each work site, six numbers an hour.

Distilling those out leaves 82 KB for every day and city collected, small
enough to commit. A deployed instance then reviews and grades finished days
with no API call at all, which matters when a single request costs thousands of
credits.

Nothing here is modelled or interpolated. Each value is the reading FortyGuard
returned for that site at that hour, carried across unchanged.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import date
from pathlib import Path

from certiroute.optimization import ConditionPoint, TemperatureProfile

DEFAULT_PROFILE_PATH = Path("data/evidence/measured_profiles.json")
DEFAULT_LEVEL_PATH = Path("data/evidence/measured_levels.json")
SCHEMA_VERSION = 1

# Matches the sentinel the live path uses: no hidden certainty penalty, because
# uncertainty is expressed through the calibrated interval instead.
NEUTRAL_CERTAINTY = 1.0


class MeasuredProfilesUnavailableError(LookupError):
    """No distilled measurements exist for this area and date."""


def load_measured_profiles(
    area_id: str, target_date: date, *, path: Path | None = None
) -> dict[str, TemperatureProfile]:
    """Return one measured profile per site, or say plainly that none exist."""

    source = path if path is not None else DEFAULT_PROFILE_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeasuredProfilesUnavailableError(
            "no distilled measurements have been built"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError("distilled measurements are not readable JSON") from exc

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported measured-profile version {payload.get('schema_version')!r}"
        )
    day = payload.get("areas", {}).get(area_id, {}).get(target_date.isoformat())
    if not day:
        raise MeasuredProfilesUnavailableError(
            f"no measured hours for {area_id} on {target_date.isoformat()}"
        )
    return {
        job_id: TemperatureProfile(
            job_id=job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=int(minute),
                    temperature_c=float(value),
                    certainty=NEUTRAL_CERTAINTY,
                )
                for minute, value in sorted(readings.items(), key=lambda kv: int(kv[0]))
            ),
        )
        for job_id, readings in day.items()
    }


def available_days(area_id: str, *, path: Path | None = None) -> tuple[date, ...]:
    """Every day this area can be reviewed offline."""

    source = path if path is not None else DEFAULT_PROFILE_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ()
    if payload.get("schema_version") != SCHEMA_VERSION:
        return ()
    return tuple(
        sorted(
            date.fromisoformat(day) for day in payload.get("areas", {}).get(area_id, {})
        )
    )


def daily_peaks(area_id: str, *, path: Path | None = None) -> dict[date, float]:
    """The hottest measured moment of each day, for choosing a limit.

    A heat ceiling is only useful if it sits inside the range the area
    actually reaches. Forty degrees never binds in Miami and binds on almost
    every Phoenix day, so a dispatcher picking one needs to see what their own
    area measured rather than guess from a number they read somewhere.
    """

    source = path if path is not None else DEFAULT_PROFILE_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    if payload.get("schema_version") != SCHEMA_VERSION:
        return {}
    return {
        date.fromisoformat(day): max(
            float(value) for site in sites.values() for value in site.values()
        )
        for day, sites in payload.get("areas", {}).get(area_id, {}).items()
        if sites
    }


class MeasuredLevelUnavailableError(LookupError):
    """No committed whole-day level exists for this area and date."""


def load_measured_level(
    area_id: str, target_date: date, *, path: Path | None = None
) -> dict[str, float]:
    """Return each site's committed whole-day level for a shipped day.

    This is the one number a plan is built from, carried across from the
    FortyGuard aggregate exactly as it arrived. It is committed rather than
    derived because the aggregate is a same-day signal: a day not captured
    while it was current can never be bought again, and a level computed some
    other way would not mean what the trained offsets were learned against.
    """

    source = path if path is not None else DEFAULT_LEVEL_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise MeasuredLevelUnavailableError(
            "no committed whole-day levels have been built"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ValueError("committed levels are not readable JSON") from exc

    if payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported level payload version {payload.get('schema_version')!r}"
        )
    day = payload.get("areas", {}).get(area_id, {}).get(target_date.isoformat())
    if not day:
        raise MeasuredLevelUnavailableError(
            f"no committed level for {area_id} on {target_date.isoformat()}"
        )
    return {job_id: float(value) for job_id, value in day.items()}


def level_days(area_id: str, *, path: Path | None = None) -> tuple[date, ...]:
    """Every day this area can be planned offline."""

    source = path if path is not None else DEFAULT_LEVEL_PATH
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return ()
    if payload.get("schema_version") != SCHEMA_VERSION:
        return ()
    return tuple(
        sorted(
            date.fromisoformat(day) for day in payload.get("areas", {}).get(area_id, {})
        )
    )


def build_level_payload(
    levels_by_area_day: Mapping[str, Mapping[date, Mapping[str, float]]],
) -> dict:
    """Shape the committed whole-day levels for writing."""

    return {
        "schema_version": SCHEMA_VERSION,
        "note": (
            "Whole-day FortyGuard aggregates at each work site - the single "
            "reading a plan is built from. Carried across unchanged; the "
            "aggregate is a same-day signal and cannot be re-bought later."
        ),
        "areas": {
            area_id: {
                day.isoformat(): {
                    job_id: round(level, 2) for job_id, level in levels.items()
                }
                for day, levels in sorted(days.items())
            }
            for area_id, days in levels_by_area_day.items()
        },
    }


def build_payload(
    profiles_by_area_day: Mapping[str, Mapping[date, Mapping[str, TemperatureProfile]]],
) -> dict:
    """Shape the distilled readings for committing."""

    return {
        "schema_version": SCHEMA_VERSION,
        "note": (
            "Measured FortyGuard readings at each work site, distilled from the "
            "heatmap cache. Values are carried across unchanged; nothing here "
            "is modelled or interpolated."
        ),
        "areas": {
            area_id: {
                day.isoformat(): {
                    job_id: {
                        str(point.minute_of_day): round(point.temperature_c, 2)
                        for point in profile.points
                    }
                    for job_id, profile in profiles.items()
                }
                for day, profiles in sorted(days.items())
            }
            for area_id, days in profiles_by_area_day.items()
        },
    }


__all__ = [
    "load_measured_level",
    "level_days",
    "build_level_payload",
    "MeasuredLevelUnavailableError",
    "DEFAULT_LEVEL_PATH",
    "DEFAULT_PROFILE_PATH",
    "SCHEMA_VERSION",
    "MeasuredProfilesUnavailableError",
    "available_days",
    "build_payload",
    "daily_peaks",
    "load_measured_profiles",
]
