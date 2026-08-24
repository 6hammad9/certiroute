"""Tests for forecast -> shift decision -> scored against reality."""

from datetime import time

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.forecasting import day_blocked_residual_scores, learn_diurnal_shape
from certiroute.optimization import ConditionPoint, TemperatureProfile
from certiroute.shift_planning import (
    recommend_shift_start,
    score_against_realization,
)

DEPOT = GeoPoint(latitude=33.4485, longitude=-112.0740)
ANCHOR = 5 * 60
CANDIDATES = (time(5, 0), time(6, 0), time(7, 0), time(8, 0))
HOURS = range(5, 18)


def profile(job_id: str, readings: dict[int, float]) -> TemperatureProfile:
    return TemperatureProfile(
        job_id=job_id,
        points=tuple(
            ConditionPoint(minute_of_day=m, temperature_c=v, certainty=1.0)
            for m, v in sorted(readings.items())
        ),
    )


def warming_day(base: float, step: float = 1.6) -> dict[int, float]:
    """A day that climbs steadily, so earlier hours are genuinely cooler."""

    return {hour * 60: base + step * (hour - 5) for hour in HOURS}


def day_for(jobs, readings) -> dict[str, TemperatureProfile]:
    return {job.job_id: profile(job.job_id, readings) for job in jobs}


@pytest.fixture
def jobs() -> list[Job]:
    return [
        Job(
            job_id=f"J{i}",
            name=f"Job {i}",
            location=GeoPoint(latitude=33.45 + 0.003 * i, longitude=-112.07),
            duration_minutes=60,
            priority=3,
        )
        for i in range(1, 4)
    ]


@pytest.fixture
def history(jobs) -> list[dict[str, TemperatureProfile]]:
    return [day_for(jobs, warming_day(base)) for base in (18.0, 19.0, 20.0)]


def test_recommendation_prefers_the_cooler_early_start(jobs, history) -> None:
    shape = learn_diurnal_shape(history[:2], anchor_minute=ANCHOR)
    scores = day_blocked_residual_scores(shape, history[2:])

    recommendation = recommend_shift_start(
        jobs,
        shape,
        anchor_temperature_c=21.0,
        calibration_scores_c=scores + [0.4, 0.9],
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
    )

    assert recommendation.recommended_start == time(5, 0)
    assert recommendation.changes_the_start
    assert recommendation.minutes_earlier == 180


def test_planning_uses_the_conservative_upper_curve(jobs, history) -> None:
    shape = learn_diurnal_shape(history[:2], anchor_minute=ANCHOR)
    scores = [1.0, 2.0, 3.0]

    cautious = recommend_shift_start(
        jobs,
        shape,
        21.0,
        scores,
        depot=DEPOT,
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
        conservative=True,
    )
    expected = recommend_shift_start(
        jobs,
        shape,
        21.0,
        scores,
        depot=DEPOT,
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
        conservative=False,
    )

    hot, _ = cautious.predicted_profiles["J1"].condition_at(10 * 60)
    mid, _ = expected.predicted_profiles["J1"].condition_at(10 * 60)
    assert hot == pytest.approx(mid + cautious.forecast.radius_c)
    assert cautious.forecast.radius_c > 0


def test_scoring_replays_candidates_on_measured_temperatures(jobs, history) -> None:
    shape = learn_diurnal_shape(history[:2], anchor_minute=ANCHOR)
    scores = day_blocked_residual_scores(shape, history[2:])
    recommendation = recommend_shift_start(
        jobs,
        shape,
        21.0,
        scores + [0.5, 0.7],
        depot=DEPOT,
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
    )

    # The day actually ran hotter than any training day.
    realized = day_for(jobs, warming_day(23.0))
    outcome = score_against_realization(
        recommendation,
        jobs,
        realized,
        depot=DEPOT,
        candidate_starts=CANDIDATES,
    )

    assert outcome.helped
    assert outcome.chose_the_best_start
    assert outcome.regret_units == pytest.approx(0.0)
    assert outcome.realized_reduction > 0.30


def test_outcome_reports_regret_when_a_better_start_existed(jobs) -> None:
    """A recommendation is scored honestly even when it is not optimal."""

    # History says mornings are cool, so the shape points early.
    history = [day_for(jobs, warming_day(18.0)), day_for(jobs, warming_day(19.0))]
    shape = learn_diurnal_shape(history, anchor_minute=ANCHOR)
    recommendation = recommend_shift_start(
        jobs,
        shape,
        20.0,
        [0.5, 0.6, 0.7],
        depot=DEPOT,
        baseline_start=time(8, 0),
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
    )

    # But the real day was hottest in the early morning and cooled off.
    inverted = {hour * 60: 40.0 - 1.2 * (hour - 5) for hour in HOURS}
    outcome = score_against_realization(
        recommendation,
        jobs,
        day_for(jobs, inverted),
        depot=DEPOT,
        candidate_starts=CANDIDATES,
    )

    assert not outcome.chose_the_best_start
    assert outcome.regret_units > 0
    assert not outcome.helped


def test_zero_exposure_baseline_reports_no_reduction_ratio(jobs) -> None:
    cool = {hour * 60: 20.0 for hour in HOURS}
    history = [day_for(jobs, cool), day_for(jobs, cool)]
    shape = learn_diurnal_shape(history, anchor_minute=ANCHOR)
    recommendation = recommend_shift_start(
        jobs,
        shape,
        20.0,
        [0.1, 0.2, 0.3],
        depot=DEPOT,
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
    )

    outcome = score_against_realization(
        recommendation,
        jobs,
        day_for(jobs, cool),
        depot=DEPOT,
        candidate_starts=CANDIDATES,
    )

    assert outcome.realized_baseline_units == 0.0
    assert outcome.realized_reduction is None


def test_scoring_refuses_when_the_recommended_start_is_infeasible(
    jobs, history
) -> None:
    shape = learn_diurnal_shape(history[:2], anchor_minute=ANCHOR)
    recommendation = recommend_shift_start(
        jobs,
        shape,
        21.0,
        [0.4, 0.5, 0.6],
        depot=DEPOT,
        candidate_starts=CANDIDATES,
        miscoverage=0.25,
    )
    realized = day_for(jobs, warming_day(23.0))

    # Scoring against a candidate set that omits the chosen start must raise
    # rather than silently substitute a different start time.
    with pytest.raises(ValueError, match="recommended start is infeasible"):
        score_against_realization(
            recommendation,
            jobs,
            realized,
            depot=DEPOT,
            candidate_starts=(time(7, 0), time(8, 0)),
        )
