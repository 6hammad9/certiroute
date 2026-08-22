"""Pure helpers for building a route scenario from map clicks.

The module has no Streamlit dependency.  ``MapScenarioState`` can be kept in
``st.session_state`` directly or round-tripped through ``to_dict`` and
``from_dict`` when a JSON-compatible value is preferred.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import time
from enum import StrEnum
from types import MappingProxyType
from typing import Final

import pandas as pd

from certiroute.job_manifest import (
    MAX_MANIFEST_JOBS,
    MIN_MANIFEST_JOBS,
    JobManifest,
    JobManifestValidation,
    validate_job_manifest,
)

DEFAULT_SHIFT_START: Final = time(8, 0)
DEFAULT_SHIFT_END: Final = time(17, 0)
DEFAULT_JOB_DURATION_MINUTES: Final = 45
DEFAULT_JOB_PRIORITY: Final = 3


@dataclass(frozen=True, slots=True)
class MapPoint:
    """One validated WGS84 point selected on the map."""

    latitude: float
    longitude: float

    def __post_init__(self) -> None:
        if isinstance(self.latitude, bool) or isinstance(self.longitude, bool):
            raise ValueError("Map coordinates must be numbers.")
        try:
            latitude = float(self.latitude)
            longitude = float(self.longitude)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Map coordinates must be numbers.") from exc
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise ValueError("Map coordinates must be finite numbers.")
        if not -90 <= latitude <= 90:
            raise ValueError("Latitude must be between -90 and 90.")
        if not -180 <= longitude <= 180:
            raise ValueError("Longitude must be between -180 and 180.")
        object.__setattr__(self, "latitude", 0.0 if latitude == 0 else latitude)
        object.__setattr__(self, "longitude", 0.0 if longitude == 0 else longitude)

    def to_dict(self) -> dict[str, float]:
        """Return a JSON-compatible representation."""

        return {"latitude": self.latitude, "longitude": self.longitude}

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MapPoint:
        """Restore a point previously returned by :meth:`to_dict`."""

        try:
            latitude = value["latitude"]
            longitude = value["longitude"]
        except (KeyError, TypeError) as exc:
            raise ValueError("Saved map point is incomplete.") from exc
        return cls(latitude=latitude, longitude=longitude)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class OperatingAreaPreset:
    """A friendly starting view for a U.S. operating area."""

    area_id: str
    label: str
    center: MapPoint
    zoom: int = 12


OPERATING_AREA_PRESETS: Final[tuple[OperatingAreaPreset, ...]] = (
    OperatingAreaPreset(
        area_id="phoenix",
        label="Phoenix, Arizona",
        center=MapPoint(33.4484, -112.0740),
    ),
    OperatingAreaPreset(
        area_id="houston",
        label="Houston, Texas",
        center=MapPoint(29.7604, -95.3698),
    ),
    OperatingAreaPreset(
        area_id="miami",
        label="Miami, Florida",
        center=MapPoint(25.7617, -80.1918),
    ),
    OperatingAreaPreset(
        area_id="las-vegas",
        label="Las Vegas, Nevada",
        center=MapPoint(36.1699, -115.1398),
    ),
    OperatingAreaPreset(
        area_id="los-angeles",
        label="Los Angeles, California",
        center=MapPoint(34.0522, -118.2437),
    ),
    OperatingAreaPreset(
        area_id="austin",
        label="Austin, Texas",
        center=MapPoint(30.2672, -97.7431),
    ),
    OperatingAreaPreset(
        area_id="denver",
        label="Denver, Colorado",
        center=MapPoint(39.7392, -104.9903),
    ),
    OperatingAreaPreset(
        area_id="atlanta",
        label="Atlanta, Georgia",
        center=MapPoint(33.7490, -84.3880),
    ),
    OperatingAreaPreset(
        area_id="chicago",
        label="Chicago, Illinois",
        center=MapPoint(41.8781, -87.6298),
    ),
    OperatingAreaPreset(
        area_id="new-york",
        label="New York, New York",
        center=MapPoint(40.7128, -74.0060),
    ),
    OperatingAreaPreset(
        area_id="seattle",
        label="Seattle, Washington",
        center=MapPoint(47.6062, -122.3321),
    ),
    OperatingAreaPreset(
        area_id="washington-dc",
        label="Washington, District of Columbia",
        center=MapPoint(38.9072, -77.0369),
    ),
)
DEFAULT_OPERATING_AREA_ID: Final = OPERATING_AREA_PRESETS[0].area_id
OPERATING_AREA_BY_ID: Final[Mapping[str, OperatingAreaPreset]] = MappingProxyType(
    {area.area_id: area for area in OPERATING_AREA_PRESETS}
)


class MapClickAction(StrEnum):
    """What happened when one map event was applied."""

    DEPOT_SET = "depot_set"
    JOB_SITE_ADDED = "job_site_added"
    DUPLICATE_IGNORED = "duplicate_ignored"
    JOB_LIMIT_REACHED = "job_limit_reached"


@dataclass(frozen=True, slots=True)
class MapScenarioState:
    """Immutable map selections plus memory of the last handled map event."""

    operating_area_id: str = DEFAULT_OPERATING_AREA_ID
    depot: MapPoint | None = None
    job_sites: tuple[MapPoint, ...] = ()
    last_click_token: str | None = None

    def __post_init__(self) -> None:
        if self.operating_area_id not in OPERATING_AREA_BY_ID:
            raise ValueError(f"Unknown operating area: {self.operating_area_id}")
        sites = tuple(self.job_sites)
        if any(not isinstance(site, MapPoint) for site in sites):
            raise ValueError("Every job site must be a MapPoint.")
        if len(sites) > MAX_MANIFEST_JOBS:
            raise ValueError(f"A scenario supports at most {MAX_MANIFEST_JOBS} jobs.")
        if self.depot is not None and not isinstance(self.depot, MapPoint):
            raise ValueError("Depot must be a MapPoint.")
        if self.last_click_token is not None and not isinstance(
            self.last_click_token, str
        ):
            raise ValueError("Last click token must be text.")
        object.__setattr__(self, "job_sites", sites)

    @property
    def operating_area(self) -> OperatingAreaPreset:
        """Return the selected area's display and initial-map settings."""

        return OPERATING_AREA_BY_ID[self.operating_area_id]

    @property
    def job_count(self) -> int:
        return len(self.job_sites)

    @property
    def can_add_job(self) -> bool:
        return self.depot is not None and self.job_count < MAX_MANIFEST_JOBS

    @property
    def is_ready(self) -> bool:
        return self.depot is not None and self.job_count >= MIN_MANIFEST_JOBS

    def to_dict(self) -> dict[str, object]:
        """Return state using only JSON-compatible primitives."""

        return {
            "schema_version": 1,
            "operating_area_id": self.operating_area_id,
            "depot": None if self.depot is None else self.depot.to_dict(),
            "job_sites": [site.to_dict() for site in self.job_sites],
            "last_click_token": self.last_click_token,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> MapScenarioState:
        """Restore and validate JSON-compatible session state."""

        try:
            area_id = value["operating_area_id"]
            depot_value = value.get("depot")
            sites_value = value.get("job_sites", [])
            last_click_token = value.get("last_click_token")
        except (AttributeError, TypeError) as exc:
            raise ValueError("Saved map scenario is invalid.") from exc
        if not isinstance(area_id, str):
            raise ValueError("Saved operating area is invalid.")
        if depot_value is not None and not isinstance(depot_value, Mapping):
            raise ValueError("Saved depot is invalid.")
        if not isinstance(sites_value, list | tuple):
            raise ValueError("Saved job sites are invalid.")
        if any(not isinstance(site, Mapping) for site in sites_value):
            raise ValueError("Saved job sites are invalid.")
        if last_click_token is not None and not isinstance(last_click_token, str):
            raise ValueError("Saved click token is invalid.")
        return cls(
            operating_area_id=area_id,
            depot=None if depot_value is None else MapPoint.from_dict(depot_value),
            job_sites=tuple(MapPoint.from_dict(site) for site in sites_value),
            last_click_token=last_click_token,
        )


@dataclass(frozen=True, slots=True)
class MapClickResult:
    """New scenario state and the outcome a UI may present to the user."""

    state: MapScenarioState
    action: MapClickAction

    @property
    def point_was_added(self) -> bool:
        return self.action in {
            MapClickAction.DEPOT_SET,
            MapClickAction.JOB_SITE_ADDED,
        }


def apply_map_click(
    state: MapScenarioState,
    *,
    latitude: float,
    longitude: float,
    event_id: str | int | None = None,
) -> MapClickResult:
    """Consume one click, setting the depot first and then adding job sites.

    Interactive map components often return their last click again on every
    Streamlit rerun.  ``event_id`` is preferred when the component supplies
    one; otherwise a canonical coordinate token suppresses the repeated event.
    """

    point = MapPoint(latitude=latitude, longitude=longitude)
    token = _click_token(point, event_id=event_id)
    if token == state.last_click_token:
        return MapClickResult(state=state, action=MapClickAction.DUPLICATE_IGNORED)

    handled_state = replace(state, last_click_token=token)
    if state.depot is None:
        return MapClickResult(
            state=replace(handled_state, depot=point),
            action=MapClickAction.DEPOT_SET,
        )
    if state.job_count >= MAX_MANIFEST_JOBS:
        return MapClickResult(
            state=handled_state,
            action=MapClickAction.JOB_LIMIT_REACHED,
        )
    return MapClickResult(
        state=replace(handled_state, job_sites=(*state.job_sites, point)),
        action=MapClickAction.JOB_SITE_ADDED,
    )


def undo_last_point(state: MapScenarioState) -> MapScenarioState:
    """Remove the newest job site, or the depot when no jobs remain.

    Click memory is deliberately retained so a map component's stale payload
    cannot immediately re-add the point during the rerun caused by Undo.
    """

    if state.job_sites:
        return replace(state, job_sites=state.job_sites[:-1])
    if state.depot is not None:
        return replace(state, depot=None)
    return state


def reset_points(state: MapScenarioState) -> MapScenarioState:
    """Clear depot and jobs while retaining area and stale-click protection."""

    if state.depot is None and not state.job_sites:
        return state
    return replace(state, depot=None, job_sites=())


def select_operating_area(state: MapScenarioState, area_id: str) -> MapScenarioState:
    """Switch the map preset and start an empty selection for that area."""

    if area_id not in OPERATING_AREA_BY_ID:
        raise ValueError(f"Unknown operating area: {area_id}")
    if area_id == state.operating_area_id:
        return state
    return MapScenarioState(
        operating_area_id=area_id,
        last_click_token=state.last_click_token,
    )


def build_default_job_manifest(
    state: MapScenarioState,
    *,
    shift_start: time = DEFAULT_SHIFT_START,
    shift_end: time = DEFAULT_SHIFT_END,
    duration_minutes: int = DEFAULT_JOB_DURATION_MINUTES,
    priority: int = DEFAULT_JOB_PRIORITY,
) -> JobManifestValidation:
    """Turn selected job sites into the standard validated job manifest."""

    rows = [
        {
            "job_id": f"SITE-{index:02d}",
            "name": f"Work site {index}",
            "latitude": site.latitude,
            "longitude": site.longitude,
            "duration_minutes": duration_minutes,
            "priority": priority,
            "earliest_start": shift_start.strftime("%H:%M"),
            "latest_finish": shift_end.strftime("%H:%M"),
        }
        for index, site in enumerate(state.job_sites, 1)
    ]
    return validate_job_manifest(pd.DataFrame(rows))


def map_scenario_fingerprint(state: MapScenarioState, manifest: JobManifest) -> str:
    """Fingerprint route-relevant map state using the validated manifest hash."""

    payload = {
        "schema": "certiroute-map-scenario-v1",
        "operating_area_id": state.operating_area_id,
        "depot": None if state.depot is None else state.depot.to_dict(),
        "job_manifest_fingerprint": manifest.fingerprint,
    }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _click_token(point: MapPoint, *, event_id: str | int | None) -> str:
    if event_id is not None and str(event_id).strip():
        return f"event:{event_id}"
    return f"coordinate:{point.latitude:.7f},{point.longitude:.7f}"


__all__ = [
    "DEFAULT_JOB_DURATION_MINUTES",
    "DEFAULT_JOB_PRIORITY",
    "DEFAULT_OPERATING_AREA_ID",
    "DEFAULT_SHIFT_END",
    "DEFAULT_SHIFT_START",
    "OPERATING_AREA_BY_ID",
    "OPERATING_AREA_PRESETS",
    "MapClickAction",
    "MapClickResult",
    "MapPoint",
    "MapScenarioState",
    "OperatingAreaPreset",
    "apply_map_click",
    "build_default_job_manifest",
    "map_scenario_fingerprint",
    "reset_points",
    "select_operating_area",
    "undo_last_point",
]
