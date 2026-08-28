"""Tests for the absolute heat limit check.

The point of the limit is that its verdict is not monotonic in the start time
the way the ranking is. These tests pin that down: a day where nothing needs
doing, a day where the shift must move, and a day where no start works at all.
"""

import pytest

from certiroute.heat_limit import (
    DayVerdict,
    assess_day_against_limit,
    check_stops_against_limit,
)
from certiroute.optimization import ConditionPoint, ScheduledStop, TemperatureProfile


def profile(job_id: str, readings: dict[int, float]) -> TemperatureProfile:
    return TemperatureProfile(
        job_id=job_id,
        points=tuple(
            ConditionPoint(minute_of_day=minute, temperature_c=value, certainty=1.0)
            for minute, value in sorted(readings.items())
        ),
    )


def stop(job_id: str, start_minute: int, finish_minute: int) -> ScheduledStop:
    return ScheduledStop(
        sequence=1,
        job_id=job_id,
        job_name=f"Site {job_id}",
        latitude=33.4,
        longitude=-112.0,
        arrival_minute=start_minute,
        start_minute=start_minute,
        finish_minute=finish_minute,
        inbound_travel_minutes=0,
        temperature_c=30.0,
        peak_temperature_c=30.0,
        certainty=1.0,
        raw_exposure_units=0.0,
        certainty_adjusted_units=0.0,
        minutes_above_planning_threshold=0.0,
    )


# A day that climbs steadily, so an earlier shift is genuinely cooler.
RISING = {5 * 60: 34.0, 11 * 60: 40.0, 17 * 60: 44.0}


def test_a_shift_entirely_under_the_limit_is_clear() -> None:
    check = check_stops_against_limit(
        [stop("A", 5 * 60, 7 * 60)],
        {"A": profile("A", RISING)},
        limit_c=40.0,
        shift_start_minute=5 * 60,
    )

    assert check.clear
    assert check.sites_over == 0
    assert check.minutes_over == 0
    assert check.peak_c == pytest.approx(36.0, abs=0.1)


def test_a_breach_reports_where_and_for_how_long() -> None:
    check = check_stops_against_limit(
        [stop("A", 11 * 60, 13 * 60)],
        {"A": profile("A", RISING)},
        limit_c=40.0,
        shift_start_minute=11 * 60,
    )

    assert not check.clear
    assert check.sites_over == 1
    breach = check.breaches[0]
    assert breach.job_name == "Site A"
    assert breach.first_minute == 11 * 60
    assert breach.minutes_over == 125  # two hours, resolved to the sampling step
    assert breach.peak_c == pytest.approx(41.3, abs=0.1)


def test_only_working_minutes_count_not_travel() -> None:
    """A stop arrived at early but started late is only exposed while working."""

    arrived_early = stop("A", 12 * 60, 13 * 60).model_copy(
        update={"arrival_minute": 9 * 60}
    )

    check = check_stops_against_limit(
        [arrived_early],
        {"A": profile("A", RISING)},
        limit_c=40.0,
        shift_start_minute=9 * 60,
    )

    assert check.breaches[0].first_minute == 12 * 60


def test_a_missing_profile_is_refused_rather_than_skipped() -> None:
    with pytest.raises(LookupError, match="B"):
        check_stops_against_limit(
            [stop("B", 5 * 60, 6 * 60)],
            {"A": profile("A", RISING)},
            limit_c=40.0,
            shift_start_minute=5 * 60,
        )


def day(readings: dict[int, float]) -> dict:
    """Three candidate starts of a two-hour shift on one site."""

    return {
        start * 60: [stop("A", start * 60, (start + 2) * 60)] for start in (5, 8, 11)
    }


def test_a_cool_day_asks_for_nothing() -> None:
    """The usual start already holds, so moving the crew would be disruption
    without a safety gain - and the tool must say so rather than always
    recommending the earliest hour."""

    assessment = assess_day_against_limit(
        day(RISING),
        {"A": profile("A", {5 * 60: 30.0, 11 * 60: 33.0, 17 * 60: 36.0})},
        limit_c=40.0,
        baseline_minute=11 * 60,
    )

    assert assessment.verdict is DayVerdict.NO_ACTION
    assert assessment.baseline.clear


def test_a_hot_day_asks_for_the_shift_to_move() -> None:
    assessment = assess_day_against_limit(
        day(RISING),
        {"A": profile("A", RISING)},
        limit_c=40.0,
        baseline_minute=11 * 60,
    )

    assert assessment.verdict is DayVerdict.MOVE_EARLIER
    assert not assessment.baseline.clear
    assert assessment.earliest_clear is not None


def test_an_extreme_day_says_no_start_works() -> None:
    """The case a rule of thumb gets wrong in the dangerous direction: moving
    the crew earlier feels like it solved the problem, and it did not."""

    assessment = assess_day_against_limit(
        day(RISING),
        {"A": profile("A", {5 * 60: 41.0, 11 * 60: 45.0, 17 * 60: 48.0})},
        limit_c=40.0,
        baseline_minute=11 * 60,
    )

    assert assessment.verdict is DayVerdict.NO_SAFE_START
    assert assessment.clear_starts == ()
    assert assessment.earliest_clear is None


def test_the_least_disruptive_clear_start_is_offered() -> None:
    """Among starts that hold, the crew should move as little as possible."""

    assessment = assess_day_against_limit(
        day(RISING),
        {"A": profile("A", {5 * 60: 34.0, 11 * 60: 39.0, 13 * 60: 42.0})},
        limit_c=40.0,
        baseline_minute=11 * 60,
    )

    assert assessment.verdict is DayVerdict.MOVE_EARLIER
    clear = assessment.earliest_clear
    assert clear is not None
    # 08:00 holds and is closer to the crew's usual start than 05:00 is.
    assert clear.shift_start_minute == 8 * 60


def test_the_baseline_must_be_among_the_candidates() -> None:
    with pytest.raises(ValueError, match="baseline"):
        assess_day_against_limit(
            day(RISING),
            {"A": profile("A", RISING)},
            limit_c=40.0,
            baseline_minute=9 * 60,
        )


def test_the_verdict_changes_with_the_day_not_just_the_start_time() -> None:
    """The reason a reading is worth buying.

    Ranking start times is monotonic in summer - earliest always wins - so a
    rule of thumb reproduces it. Whether any start clears an absolute limit is
    not monotonic in anything a dispatcher can see: it turns on the day's
    level. Run against real Phoenix measurements, the same question comes back
    three different ways, which is what makes it worth asking.
    """

    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    days = json.loads(
        (root / "data" / "evidence" / "measured_profiles.json").read_text(
            encoding="utf-8"
        )
    )["areas"]["phoenix"]

    def peak_over_shift(sites: dict, start_hour: int) -> float:
        minutes = [str((start_hour + hour) * 60) for hour in range(6)]
        return max(
            value
            for site in sites.values()
            for minute in minutes
            if (value := site.get(minute)) is not None
        )

    verdicts = set()
    for sites in days.values():
        early, usual = peak_over_shift(sites, 5), peak_over_shift(sites, 8)
        if usual < 40.0:
            verdicts.add(DayVerdict.NO_ACTION)
        elif early < 40.0:
            verdicts.add(DayVerdict.MOVE_EARLIER)
        else:
            verdicts.add(DayVerdict.NO_SAFE_START)

    assert verdicts == {
        DayVerdict.NO_ACTION,
        DayVerdict.MOVE_EARLIER,
        DayVerdict.NO_SAFE_START,
    }
