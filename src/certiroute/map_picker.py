"""Folium adapter for the guided, click-first route setup.

The state transition itself lives in :mod:`certiroute.map_scenario`.  Keeping
the custom Streamlit component behind this small adapter gives the app one
mockable boundary and lets the selection rules remain ordinary unit-tested
Python.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html import escape
from math import cos, radians
from typing import Any, Final

import folium
from streamlit_folium import st_folium

from certiroute.map_scenario import MapPoint, OperatingAreaPreset

DEPOT_COLOR: Final = "#70FFD2"
JOB_COLOR: Final = "#FF9137"
MARKER_TEXT_COLOR: Final = "#0B1524"
COVERAGE_COLOR: Final = "#0A7D5E"


@dataclass(frozen=True)
class MapHint:
    """A pulsing target telling the operator the map is theirs to click.

    An untouched map is a pale rectangle, and a pale rectangle looks like a
    picture of a city rather than a control. Nothing else on the page says
    "this is the input" - the instruction above it can be read as a caption -
    so the invitation has to live on the map itself.
    """

    latitude: float
    longitude: float
    text: str
    color: str = DEPOT_COLOR
    ink: str = "#05372A"


@dataclass(frozen=True)
class CoverageArea:
    """Where a trained heat model may legitimately be applied."""

    centre: MapPoint
    radius_km: float
    label: str

    def __post_init__(self) -> None:
        if self.radius_km <= 0:
            raise ValueError("Coverage radius must be greater than zero.")


def build_map_picker(
    operating_area: OperatingAreaPreset,
    *,
    depot: MapPoint | None,
    job_sites: Sequence[MapPoint],
    coverage: CoverageArea | None = None,
    hint: MapHint | None = None,
) -> tuple[folium.Map, folium.FeatureGroup]:
    """Build a stable base map and a replaceable selection overlay.

    Markers are deliberately kept out of the base map.  ``st_folium`` hashes
    the base Leaflet program to identify its component, while
    ``feature_group_to_add`` can change without remounting the map.  That keeps
    the user's pan and zoom position stable as clicks add markers.
    """

    # A 60 km boundary is entirely off-screen at a city working zoom, so an
    # untouched map opens framed on the whole covered area: the limit is the
    # first thing seen rather than something discovered by being refused. Once
    # a base is placed the map keeps the operator's own view.
    show_whole_area = coverage is not None and depot is None and not job_sites
    base_map = folium.Map(
        location=(
            [coverage.centre.latitude, coverage.centre.longitude]
            if show_whole_area and coverage is not None
            else [operating_area.center.latitude, operating_area.center.longitude]
        ),
        zoom_start=operating_area.zoom,
        tiles="CartoDB positron",
        control_scale=True,
        prefer_canvas=True,
        # Two clicks in quick succession are a double-click, and Leaflet turns
        # that into a zoom rather than two map clicks - so placing sites at a
        # normal pace silently loses every second one. Zooming stays available
        # through the controls and the scroll wheel, neither of which is in the
        # way of the interaction this map exists for.
        doubleClickZoom=False,
    )
    if show_whole_area and coverage is not None:
        # Framing the whole 60 km circle shows the boundary but reduces the city
        # to a pale smudge, which is not a scale anyone can place a work site
        # at. Just over half of it keeps streets legible while the edge stays
        # in view.
        base_map.fit_bounds(_coverage_bounds(coverage, fraction=0.58))
    base_map.get_root().header.add_child(folium.Element(_MAP_STYLE))
    selections = folium.FeatureGroup(
        name="Selected route points",
        overlay=True,
        control=False,
    )

    if coverage is not None:
        # Drawing the limit is the honest alternative to letting someone place
        # work outside it and only then be refused. The boundary is a property
        # of the trained model, not a licence area, so it is shown as a soft
        # edge rather than a hard wall.
        boundary = folium.Circle(
            location=[coverage.centre.latitude, coverage.centre.longitude],
            radius=coverage.radius_km * 1000,
            color=COVERAGE_COLOR,
            weight=1.5,
            opacity=0.55,
            dash_array="6 6",
            fill=True,
            fill_color=DEPOT_COLOR,
            fill_opacity=0.07,
        )
        # folium's path_options builds a fixed dictionary and drops anything it
        # does not know, so passing interactive to the constructor is silently
        # ignored. It has to be set on the options that reach Leaflet. It
        # matters: a filled 60 km shape is hit-tested before the map underneath
        # it, and the crew works inside it.
        boundary.options["interactive"] = False
        boundary.add_to(selections)

    if hint is not None:
        folium.Marker(
            location=[hint.latitude, hint.longitude],
            icon=folium.DivIcon(
                icon_size=(230, 230),
                icon_anchor=(115, 115),
                html=_hint_html(hint),
                class_name="cr-passthrough",
            ),
        ).add_to(selections)

    if depot is not None:
        folium.Marker(
            location=[depot.latitude, depot.longitude],
            tooltip="Crew start and finish",
            icon=folium.DivIcon(
                icon_size=(54, 36),
                icon_anchor=(27, 18),
                html=_depot_marker_html(),
                class_name="cr-passthrough",
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
                class_name="cr-passthrough",
            ),
        ).add_to(selections)

    return base_map, selections


def render_map_picker(
    operating_area: OperatingAreaPreset,
    *,
    depot: MapPoint | None,
    job_sites: Sequence[MapPoint],
    coverage: CoverageArea | None = None,
    hint: MapHint | None = None,
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
        coverage=coverage,
        hint=hint,
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


def _coverage_bounds(
    coverage: CoverageArea, *, fraction: float = 1.0
) -> list[list[float]]:
    """A latitude/longitude box containing this share of the coverage circle."""

    if fraction <= 0:
        raise ValueError("fraction must be greater than zero")
    radius = coverage.radius_km * fraction
    latitude_degrees = radius / 110.574
    longitude_degrees = radius / (
        111.320 * max(cos(radians(coverage.centre.latitude)), 1e-6)
    )
    return [
        [
            coverage.centre.latitude - latitude_degrees,
            coverage.centre.longitude - longitude_degrees,
        ],
        [
            coverage.centre.latitude + latitude_degrees,
            coverage.centre.longitude + longitude_degrees,
        ],
    ]


# The picker renders inside its own iframe, so this has to travel with the
# map rather than living in the page stylesheet.
_MAP_STYLE = """
<style>
.leaflet-container { cursor: crosshair !important; }
.leaflet-container .leaflet-control-zoom a { cursor: pointer !important; }
/* Leaflet makes every marker icon interactive, so a marker sitting over the
   map swallows the click the map is waiting for. The hint is 230px across and
   centred, which made most of the first click area dead; the placed markers
   did the same over themselves. Nothing here is meant to be clicked - the map
   underneath is - so every icon lets the click through. */
.cr-passthrough,
.cr-passthrough * { pointer-events: none !important; }
.cr-hint { pointer-events: none; text-align: center; }
.cr-hint-ring {
  position: absolute; left: 50%; top: 50%; width: 26px; height: 26px;
  margin: -13px 0 0 -13px; border-radius: 50%;
  border: 2px solid var(--cr-hint); opacity: 0;
  animation: cr-pulse 2.4s ease-out infinite;
}
.cr-hint-ring:nth-child(2) { animation-delay: .8s; }
.cr-hint-ring:nth-child(3) { animation-delay: 1.6s; }
.cr-hint-dot {
  position: absolute; left: 50%; top: 50%; width: 13px; height: 13px;
  margin: -6.5px 0 0 -6.5px; border-radius: 50%;
  background: var(--cr-hint); border: 2px solid #fff;
  box-shadow: 0 2px 8px rgba(12,17,22,.28);
}
.cr-hint-label {
  position: absolute; left: 50%; top: 50%; transform: translate(-50%, -46px);
  white-space: nowrap; padding: .34rem .62rem; border-radius: 999px;
  background: var(--cr-hint); color: var(--cr-hint-ink);
  font: 600 11.5px/1 Inter, system-ui, sans-serif; letter-spacing: .01em;
  box-shadow: 0 2px 10px rgba(12,17,22,.22);
}
@keyframes cr-pulse {
  0%   { transform: scale(.6);  opacity: .85; }
  70%  { transform: scale(3.6); opacity: 0; }
  100% { transform: scale(3.6); opacity: 0; }
}
@media (prefers-reduced-motion: reduce) {
  .cr-hint-ring { animation: none; opacity: .5; transform: scale(2.2); }
}
</style>
"""


def _hint_html(hint: MapHint) -> str:
    """A pulsing target with a label, which never intercepts a map click."""

    return (
        f'<div class="cr-hint" style="--cr-hint:{hint.color};'
        f'--cr-hint-ink:{hint.ink}">'
        '<div class="cr-hint-ring"></div>'
        '<div class="cr-hint-ring"></div>'
        '<div class="cr-hint-ring"></div>'
        '<div class="cr-hint-dot"></div>'
        f'<div class="cr-hint-label">{escape(hint.text)}</div>'
        "</div>"
    )


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
    "COVERAGE_COLOR",
    "MapHint",
    "CoverageArea",
    "DEPOT_COLOR",
    "JOB_COLOR",
    "MARKER_TEXT_COLOR",
    "build_map_picker",
    "render_map_picker",
]
