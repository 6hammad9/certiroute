from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from certiroute.job_manifest import MAX_MANIFEST_JOBS
from certiroute.map_scenario import (
    DEFAULT_JOB_DURATION_MINUTES,
    DEFAULT_JOB_PRIORITY,
    DEFAULT_OPERATING_AREA_ID,
    DEFAULT_SHIFT_END,
    DEFAULT_SHIFT_START,
    OPERATING_AREA_BY_ID,
    OPERATING_AREA_PRESETS,
    MapClickAction,
    MapPoint,
    MapScenarioState,
    apply_map_click,
    build_default_job_manifest,
    map_scenario_fingerprint,
    reset_points,
    select_operating_area,
    undo_last_point,
)


def add_click(
    state: MapScenarioState,
    latitude: float,
    longitude: float,
    event_id: str | int | None = None,
) -> MapScenarioState:
    return apply_map_click(
        state,
        latitude=latitude,
        longitude=longitude,
        event_id=event_id,
    ).state


def ready_state() -> MapScenarioState:
    state = add_click(MapScenarioState(), 33.44855, -112.07391, "depot")
    state = add_click(state, 33.44965, -112.04760, "site-1")
    return add_click(state, 33.43720, -112.05840, "site-2")


def test_operating_area_presets_are_unique_and_have_a_default() -> None:
    ids = [area.area_id for area in OPERATING_AREA_PRESETS]

    assert len(ids) == len(set(ids))
    assert DEFAULT_OPERATING_AREA_ID in OPERATING_AREA_BY_ID
    assert all(area.label and area.zoom > 0 for area in OPERATING_AREA_PRESETS)


def test_first_click_sets_depot_and_later_clicks_add_numbered_sites() -> None:
    empty = MapScenarioState()

    depot_result = apply_map_click(
        empty,
        latitude=33.44855,
        longitude=-112.07391,
        event_id="click-1",
    )
    site_result = apply_map_click(
        depot_result.state,
        latitude=33.44965,
        longitude=-112.04760,
        event_id="click-2",
    )

    assert depot_result.action is MapClickAction.DEPOT_SET
    assert depot_result.point_was_added
    assert depot_result.state.depot == MapPoint(33.44855, -112.07391)
    assert site_result.action is MapClickAction.JOB_SITE_ADDED
    assert site_result.point_was_added
    assert site_result.state.job_sites == (MapPoint(33.44965, -112.04760),)


def test_repeated_component_event_is_consumed_only_once() -> None:
    first = apply_map_click(
        MapScenarioState(),
        latitude=33.45,
        longitude=-112.07,
        event_id="component-event-17",
    )
    repeated = apply_map_click(
        first.state,
        latitude=33.45,
        longitude=-112.07,
        event_id="component-event-17",
    )

    assert repeated.action is MapClickAction.DUPLICATE_IGNORED
    assert not repeated.point_was_added
    assert repeated.state is first.state
    assert repeated.state.job_sites == ()


def test_coordinates_are_a_stable_fallback_when_map_has_no_event_id() -> None:
    first = apply_map_click(
        MapScenarioState(), latitude=33.450000001, longitude=-112.070000001
    )
    repeated = apply_map_click(
        first.state, latitude=33.450000002, longitude=-112.070000002
    )

    assert repeated.action is MapClickAction.DUPLICATE_IGNORED
    assert repeated.state.job_count == 0


def test_job_limit_is_enforced_and_the_extra_event_is_consumed() -> None:
    state = add_click(MapScenarioState(), 33.40, -112.00, "depot")
    for index in range(MAX_MANIFEST_JOBS):
        state = add_click(
            state,
            33.41 + index * 0.001,
            -112.01 - index * 0.001,
            f"site-{index}",
        )

    result = apply_map_click(
        state,
        latitude=33.5,
        longitude=-112.1,
        event_id="too-many",
    )
    repeated = apply_map_click(
        result.state,
        latitude=33.5,
        longitude=-112.1,
        event_id="too-many",
    )

    assert result.action is MapClickAction.JOB_LIMIT_REACHED
    assert result.state.job_count == MAX_MANIFEST_JOBS
    assert result.state.last_click_token == "event:too-many"
    assert repeated.action is MapClickAction.DUPLICATE_IGNORED


def test_undo_removes_sites_then_depot_without_replaying_stale_click() -> None:
    state = ready_state()
    stale_token = state.last_click_token

    one_site = undo_last_point(state)
    no_sites = undo_last_point(one_site)
    no_depot = undo_last_point(no_sites)

    assert one_site.job_count == 1
    assert no_sites.job_count == 0
    assert no_depot.depot is None
    assert no_depot.last_click_token == stale_token
    assert undo_last_point(no_depot) is no_depot


def test_reset_keeps_selected_area_and_stale_click_protection() -> None:
    state = select_operating_area(ready_state(), "miami")
    state = add_click(state, 25.76, -80.19, "miami-depot")
    cleared = reset_points(state)

    assert cleared.operating_area_id == "miami"
    assert cleared.depot is None
    assert cleared.job_sites == ()
    assert cleared.last_click_token == "event:miami-depot"


def test_selecting_another_area_starts_a_clean_scenario() -> None:
    state = ready_state()

    changed = select_operating_area(state, "houston")

    assert changed.operating_area_id == "houston"
    assert changed.depot is None
    assert changed.job_sites == ()
    assert select_operating_area(changed, "houston") is changed


def test_state_is_immutable_and_json_round_trippable() -> None:
    state = ready_state()
    serialized = json.loads(json.dumps(state.to_dict()))
    restored = MapScenarioState.from_dict(serialized)

    assert restored == state
    with pytest.raises(FrozenInstanceError):
        state.operating_area_id = "miami"  # type: ignore[misc]


def test_default_manifest_uses_plain_names_and_full_shift_defaults() -> None:
    validation = build_default_job_manifest(ready_state())

    assert validation.is_valid
    assert validation.manifest is not None
    frame = validation.manifest.frame
    assert frame["job_id"].tolist() == ["SITE-01", "SITE-02"]
    assert frame["name"].tolist() == ["Work site 1", "Work site 2"]
    assert frame["duration_minutes"].tolist() == [
        DEFAULT_JOB_DURATION_MINUTES,
        DEFAULT_JOB_DURATION_MINUTES,
    ]
    assert frame["priority"].tolist() == [DEFAULT_JOB_PRIORITY, DEFAULT_JOB_PRIORITY]
    assert (
        frame["earliest_start"].tolist() == [DEFAULT_SHIFT_START.strftime("%H:%M")] * 2
    )
    assert frame["latest_finish"].tolist() == [DEFAULT_SHIFT_END.strftime("%H:%M")] * 2


def test_default_manifest_requires_at_least_two_selected_jobs() -> None:
    state = add_click(MapScenarioState(), 33.45, -112.07, "depot")
    state = add_click(state, 33.46, -112.08, "site")

    validation = build_default_job_manifest(state)

    assert not validation.is_valid
    assert any("between 2 and 9 jobs" in issue for issue in validation.error_messages)


def test_manifest_and_scenario_fingerprints_are_stable_after_round_trip() -> None:
    state = ready_state()
    restored = MapScenarioState.from_dict(state.to_dict())
    first = build_default_job_manifest(state)
    second = build_default_job_manifest(restored)

    assert first.manifest is not None
    assert second.manifest is not None
    assert first.manifest.fingerprint == second.manifest.fingerprint
    assert map_scenario_fingerprint(state, first.manifest) == map_scenario_fingerprint(
        restored, second.manifest
    )
    moved_depot = MapScenarioState(
        operating_area_id=state.operating_area_id,
        depot=MapPoint(33.50, -112.10),
        job_sites=state.job_sites,
    )
    assert map_scenario_fingerprint(state, first.manifest) != map_scenario_fingerprint(
        moved_depot, first.manifest
    )


@pytest.mark.parametrize(
    ("latitude", "longitude"),
    [(91, 0), (-91, 0), (0, 181), (0, -181), (float("nan"), 0), (True, 0)],
)
def test_invalid_map_coordinates_are_rejected(latitude, longitude) -> None:
    with pytest.raises(ValueError):
        apply_map_click(MapScenarioState(), latitude=latitude, longitude=longitude)


def test_unknown_or_corrupt_saved_state_is_rejected() -> None:
    with pytest.raises(ValueError, match="Unknown operating area"):
        select_operating_area(MapScenarioState(), "not-a-city")
    with pytest.raises(ValueError, match="Saved job sites"):
        MapScenarioState.from_dict(
            {"operating_area_id": "phoenix", "job_sites": "not-a-list"}
        )
