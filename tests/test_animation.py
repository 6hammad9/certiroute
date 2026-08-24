"""Tests for the day playback.

None of this can be checked by reading the rendered page from here, so the
data the animation runs on is pinned instead: the geometry it draws, the
timeline it plays, and the fact that it ships without reaching for anything.
"""

import json

import pytest

from certiroute.animation import (
    HEAT_COLOR,
    ROUTE_COLOR,
    VIEW_HEIGHT,
    VIEW_WIDTH,
    PlaybackRun,
    build_playback_payload,
    route_playback_html,
)
from certiroute.domain import GeoPoint
from certiroute.optimization import (
    ConditionPoint,
    ScheduledStop,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
)

DEPOT = GeoPoint(latitude=33.4485, longitude=-112.0740)


def stop(sequence: int, *, arrive: int, minutes: int = 60, travel: int = 10):
    return ScheduledStop(
        sequence=sequence,
        job_id=f"J{sequence}",
        job_name=f"Site {sequence} — demo inspection",
        latitude=33.4485 + 0.004 * sequence,
        longitude=-112.0740 + 0.006 * sequence,
        arrival_minute=arrive,
        start_minute=arrive,
        finish_minute=arrive + minutes,
        inbound_travel_minutes=travel,
        temperature_c=30.0 + sequence,
        peak_temperature_c=31.0 + sequence,
        certainty=1.0,
        raw_exposure_units=5.0 * sequence,
        certainty_adjusted_units=5.0 * sequence,
        minutes_above_planning_threshold=float(minutes),
    )


def plan_from(first_arrival: int, *, strategy=ScheduleStrategy.HEAT_AWARE):
    stops = tuple(
        stop(index, arrive=first_arrival + (index - 1) * 70)
        for index in range(1, 4)
    )
    return SchedulePlan(
        strategy=strategy,
        stops=stops,
        total_travel_minutes=40,
        total_raw_exposure_units=100.0,
        total_adjusted_exposure_units=100.0,
        minutes_above_planning_threshold=120.0,
        priority_weighted_delay_minutes=0.0,
        route_finish_minute=stops[-1].finish_minute + 20,
        objective_value=100.0,
    )


def profiles_for(plan: SchedulePlan):
    return {
        item.job_id: TemperatureProfile(
            job_id=item.job_id,
            points=tuple(
                ConditionPoint(
                    minute_of_day=hour * 60,
                    temperature_c=26.0 + 1.4 * (hour - 5),
                    certainty=1.0,
                )
                for hour in range(5, 18)
            ),
        )
        for item in plan.stops
    }


@pytest.fixture
def two_runs():
    early, late = plan_from(300), plan_from(480)
    return [
        PlaybackRun("Recommended 05:00", early, ROUTE_COLOR, recommended=True),
        PlaybackRun("Your usual 08:00", late, HEAT_COLOR),
    ]


def test_the_timeline_spans_the_first_departure_to_the_last_return(two_runs):
    payload = build_playback_payload(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )

    # The early crew leaves first; the late crew is the one still out at the end.
    assert payload["from"] == two_runs[0].plan.stops[0].arrival_minute - 10
    assert payload["to"] == two_runs[1].plan.route_finish_minute
    assert payload["to"] > payload["from"]


def test_the_recommended_crew_gets_home_before_the_usual_one(two_runs):
    """The whole point of the animation, stated as a fact about its data."""

    payload = build_playback_payload(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )
    recommended, usual = payload["runs"]

    assert recommended["recommended"] is True
    assert recommended["finish"] < usual["finish"]
    assert recommended["depart"] < usual["depart"]


def test_every_drawn_point_lands_inside_the_canvas(two_runs):
    payload = build_playback_payload(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )
    points = [(payload["depot"]["x"], payload["depot"]["y"])] + [
        (item["x"], item["y"])
        for run in payload["runs"]
        for item in run["stops"]
    ]

    assert points
    for x, y in points:
        assert 0 <= x <= VIEW_WIDTH
        assert 0 <= y <= VIEW_HEIGHT


def test_both_runs_share_one_projection_of_the_same_sites(two_runs):
    """Two crews on the same route must be drawn on the same geometry."""

    payload = build_playback_payload(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )
    recommended, usual = payload["runs"]

    for early, late in zip(recommended["stops"], usual["stops"], strict=True):
        assert early["x"] == late["x"]
        assert early["y"] == late["y"]


def test_a_single_run_still_plays(two_runs):
    payload = build_playback_payload(
        two_runs[:1], profiles_for(two_runs[0].plan), depot=DEPOT
    )

    assert len(payload["runs"]) == 1
    assert payload["from"] < payload["to"]


def test_no_runs_is_refused():
    with pytest.raises(ValueError, match="at least one run"):
        build_playback_payload([], {}, depot=DEPOT)


def test_a_run_without_stops_is_refused():
    empty = SchedulePlan(
        strategy=ScheduleStrategy.HEAT_AWARE,
        stops=(),
        total_travel_minutes=0,
        total_raw_exposure_units=0.0,
        total_adjusted_exposure_units=0.0,
        minutes_above_planning_threshold=0.0,
        priority_weighted_delay_minutes=0.0,
        route_finish_minute=0,
        objective_value=0.0,
    )

    with pytest.raises(ValueError, match="at least one stop"):
        build_playback_payload(
            [PlaybackRun("Empty", empty, ROUTE_COLOR)], {}, depot=DEPOT
        )


def test_the_document_is_self_contained(two_runs):
    """It has to run on a demo machine with no network and no CDN."""

    markup = route_playback_html(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )

    assert "__PAYLOAD__" not in markup
    assert "<svg" in markup and "requestAnimationFrame" in markup
    for forbidden in ("https://", "<script src", "<link", "@import", "fetch("):
        assert forbidden not in markup
    # The one http:// present is the SVG namespace identifier, which names a
    # standard rather than fetching anything.
    assert markup.count("http://") == 1
    assert "http://www.w3.org/2000/svg" in markup


def test_the_payload_embedded_in_the_document_is_valid_json(two_runs):
    markup = route_playback_html(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )
    raw = markup.split("const DATA = ", 1)[1].split(";\n", 1)[0]

    payload = json.loads(raw)
    assert len(payload["runs"]) == 2
    assert payload["threshold"] == pytest.approx(35.0)


def test_the_playback_uses_only_the_product_palette(two_runs):
    markup = route_playback_html(
        two_runs, profiles_for(two_runs[0].plan), depot=DEPOT
    )

    assert ROUTE_COLOR in markup
    assert HEAT_COLOR in markup
