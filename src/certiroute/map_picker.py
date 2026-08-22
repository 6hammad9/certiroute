"""Folium adapter for the guided, click-first route setup.

The state transition itself lives in :mod:`certiroute.map_scenario`.  Keeping
the custom Streamlit component behind this small adapter gives the app one
mockable boundary and lets the selection rules remain ordinary unit-tested
Python.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Final

import folium
from streamlit_folium import st_folium

from certiroute.map_scenario import MapPoint, OperatingAreaPreset

DEPOT_COLOR: Final = "#70FFD2"
JOB_COLOR: Final = "#FF9137"
MARKER_TEXT_COLOR: Final = "#0B1524"


def build_map_picker(
    operating_area: OperatingAreaPreset,
    *,
    depot: MapPoint | None,
    job_sites: Sequence[MapPoint],
) -> tuple[folium.Map, folium.FeatureGroup]:
    """Build a stable base map and a replaceable selection overlay.

    Markers are deliberately kept out of the base map.  ``st_folium`` hashes
    the base Leaflet program to identify its component, while
    ``feature_group_to_add`` can change without remounting the map.  That keeps
    the user's pan and zoom position stable as clicks add markers.
    """

    base_map = folium.Map(
        location=[operating_area.center.latitude, operating_area.center.longitude],
        zoom_start=operating_area.zoom,
        tiles="CartoDB dark_matter",
        control_scale=True,
        prefer_canvas=True,
    )
    selections = folium.FeatureGroup(
        name="Selected route points",
        overlay=True,
        control=False,
    )

    if depot is not None:
        folium.Marker(
            location=[depot.latitude, depot.longitude],
            tooltip="Crew start and finish",
            icon=folium.DivIcon(
                icon_size=(54, 36),
                icon_anchor=(27, 18),
                html=_depot_marker_html(),
            ),
        ).add_to(selections)

    for sequence, site in enumerate(job_sites, start=1):
        folium.Marker(
            location=[site.latitude, site.longitude],
            tooltip=f"Work location {sequence}",
            icon=folium.DivIcon(
                icon_size=(36, 36),
                icon_anchor=(18, 18),
                html=_job_marker_html(sequence),
            ),
        ).add_to(selections)

    return base_map, selections


def render_map_picker(
    operating_area: OperatingAreaPreset,
    *,
    depot: MapPoint | None,
    job_sites: Sequence[MapPoint],
    generation: int = 0,
    height: int = 500,
) -> Mapping[str, Any]:
    """Render the bidirectional picker and return its most recent click.

    ``generation`` is an explicit escape hatch for a deliberate component
    reset.  Normal point additions must keep it unchanged so the key and map
    viewport remain stable.
    """

    if isinstance(generation, bool) or generation < 0:
        raise ValueError("Map generation must be a non-negative integer.")
    if isinstance(height, bool) or height < 280:
        raise ValueError("Map height must be at least 280 pixels.")

    base_map, selections = build_map_picker(
        operating_area,
        depot=depot,
        job_sites=job_sites,
    )
    result = st_folium(
        base_map,
        key=f"certiroute-work-map-{operating_area.area_id}-{generation}",
        height=height,
        use_container_width=True,
        returned_objects=["last_clicked"],
        return_on_hover=False,
        feature_group_to_add=selections,
    )
    return result if isinstance(result, Mapping) else {}


def _depot_marker_html() -> str:
    return f"""
    <div aria-label="Crew start and finish" style="
      width:54px;height:34px;border-radius:4px;background:{DEPOT_COLOR};
      border:3px solid white;box-shadow:0 3px 10px rgba(112,255,210,.28);
      color:{MARKER_TEXT_COLOR};font:800 10px/28px sans-serif;letter-spacing:.06em;
      text-align:center;box-sizing:border-box;">START</div>
    """


def _job_marker_html(sequence: int) -> str:
    return f"""
    <div aria-label="Work location {sequence}" style="
      width:34px;height:34px;border-radius:50%;background:{JOB_COLOR};
      border:3px solid white;box-shadow:0 3px 10px rgba(255,145,55,.3);
      color:{MARKER_TEXT_COLOR};font:900 15px/28px sans-serif;text-align:center;
      box-sizing:border-box;">{sequence}</div>
    """


__all__ = [
    "DEPOT_COLOR",
    "JOB_COLOR",
    "MARKER_TEXT_COLOR",
    "build_map_picker",
    "render_map_picker",
]
