from __future__ import annotations

from typing import Any

import folium
import pytest

import certiroute.map_picker as map_picker
from certiroute.map_scenario import OPERATING_AREA_BY_ID, MapPoint


def phoenix_area():
    return OPERATING_AREA_BY_ID["phoenix"]


def test_picker_keeps_dynamic_selection_out_of_the_base_map() -> None:
    depot = MapPoint(33.44855, -112.07391)
    sites = (MapPoint(33.44965, -112.04760), MapPoint(33.43720, -112.05840))

    base_map, selections = map_picker.build_map_picker(
        phoenix_area(),
        depot=depot,
        job_sites=sites,
    )

    assert base_map.location == pytest.approx([33.4484, -112.0740])
    assert map_picker.DEPOT_COLOR == "#70FFD2"
    assert map_picker.JOB_COLOR == "#FF9137"
    assert map_picker.MARKER_TEXT_COLOR == "#0B1524"
    tile_layer = next(
        child
        for child in base_map._children.values()
        if isinstance(child, folium.TileLayer)
    )
    assert "light_all" in tile_layer.tiles
    assert not any(
        isinstance(child, folium.Marker) for child in base_map._children.values()
    )
    markers = [
        child for child in selections._children.values() if type(child) is folium.Marker
    ]
    assert len(markers) == 3  # depot plus two work locations
    assert not any(
        isinstance(child, folium.Circle) for child in selections._children.values()
    )

    marker_html = " ".join(marker.icon.options["html"] for marker in markers)
    assert map_picker.DEPOT_COLOR in marker_html
    assert map_picker.JOB_COLOR in marker_html
    assert map_picker.MARKER_TEXT_COLOR in marker_html
    assert "START" in marker_html
    assert "Work location 1" in marker_html
    assert "Work location 2" in marker_html


def test_render_uses_one_stable_click_only_component_boundary(monkeypatch) -> None:
    calls: list[dict[str, Any]] = []
    click = {"last_clicked": {"lat": 33.45, "lng": -112.07}}

    def fake_st_folium(_map: folium.Map, **kwargs: Any):
        calls.append(kwargs)
        return click

    monkeypatch.setattr(map_picker, "st_folium", fake_st_folium)
    first = map_picker.render_map_picker(
        phoenix_area(),
        depot=MapPoint(33.44855, -112.07391),
        job_sites=(MapPoint(33.44965, -112.04760),),
        generation=4,
    )
    second = map_picker.render_map_picker(
        phoenix_area(),
        depot=MapPoint(33.44855, -112.07391),
        job_sites=(
            MapPoint(33.44965, -112.04760),
            MapPoint(33.43720, -112.05840),
        ),
        generation=4,
    )

    assert first == click
    assert second == click
    assert [call["key"] for call in calls] == [
        "certiroute-work-map-phoenix-4",
        "certiroute-work-map-phoenix-4",
    ]
    assert all(call["returned_objects"] == ["last_clicked"] for call in calls)
    assert all(call["return_on_hover"] is False for call in calls)
    assert all(call["use_container_width"] is True for call in calls)
    assert all(
        isinstance(call["feature_group_to_add"], folium.FeatureGroup) for call in calls
    )


def test_area_or_generation_change_deliberately_changes_component_key(
    monkeypatch,
) -> None:
    keys: list[str] = []

    def fake_st_folium(_map: folium.Map, **kwargs: Any):
        keys.append(kwargs["key"])
        return {"last_clicked": None}

    monkeypatch.setattr(map_picker, "st_folium", fake_st_folium)
    for area_id, generation in (("phoenix", 0), ("phoenix", 1), ("miami", 1)):
        map_picker.render_map_picker(
            OPERATING_AREA_BY_ID[area_id],
            depot=None,
            job_sites=(),
            generation=generation,
        )

    assert keys == [
        "certiroute-work-map-phoenix-0",
        "certiroute-work-map-phoenix-1",
        "certiroute-work-map-miami-1",
    ]


@pytest.mark.parametrize(
    ("generation", "height"),
    [(-1, 500), (True, 500), (0, 279), (0, False)],
)
def test_invalid_component_lifecycle_values_are_rejected(generation, height) -> None:
    with pytest.raises(ValueError):
        map_picker.render_map_picker(
            phoenix_area(),
            depot=None,
            job_sites=(),
            generation=generation,
            height=height,
        )


# --- Showing where a trained model may be applied ---------------------------


def phoenix_coverage(radius_km: float = 60.0) -> map_picker.CoverageArea:
    return map_picker.CoverageArea(
        centre=MapPoint(33.4430, -112.0152),
        radius_km=radius_km,
        label="Phoenix, Arizona",
    )


def test_coverage_draws_a_circle_the_user_can_see() -> None:
    """Showing the limit beats refusing work only after it is set up."""

    _, selections = map_picker.build_map_picker(
        phoenix_area(),
        depot=None,
        job_sites=(),
        coverage=phoenix_coverage(),
    )
    circles = [
        child
        for child in selections._children.values()
        if child.__class__.__name__ == "Circle"
    ]

    assert len(circles) == 1
    # Folium takes metres; the model's limit is expressed in kilometres.
    assert circles[0].options["radius"] == pytest.approx(60_000)
    assert circles[0].location == [pytest.approx(33.4430), pytest.approx(-112.0152)]


def test_no_coverage_draws_no_circle() -> None:
    """An untrained area must not imply a boundary it does not have."""

    _, selections = map_picker.build_map_picker(
        phoenix_area(), depot=None, job_sites=(), coverage=None
    )

    assert not [
        child
        for child in selections._children.values()
        if child.__class__.__name__ == "Circle"
    ]


def test_the_circle_names_the_area_and_its_radius() -> None:
    _, selections = map_picker.build_map_picker(
        phoenix_area(), depot=None, job_sites=(), coverage=phoenix_coverage()
    )
    circle = next(
        child
        for child in selections._children.values()
        if child.__class__.__name__ == "Circle"
    )

    tooltip = str(next(iter(circle._children.values())).text)
    assert "Phoenix, Arizona" in tooltip
    assert "60 km" in tooltip


@pytest.mark.parametrize("radius", [0.0, -1.0])
def test_a_meaningless_radius_is_refused(radius: float) -> None:
    with pytest.raises(ValueError, match="greater than zero"):
        map_picker.CoverageArea(
            centre=MapPoint(33.44, -112.07), radius_km=radius, label="X"
        )
