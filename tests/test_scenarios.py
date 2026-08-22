from datetime import time

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.optimization.models import (
    ConditionPoint,
    ScheduleStrategy,
    TemperatureProfile,
)
from certiroute.optimization.scenarios import (
    ChanceConstraint,
    ChanceConstraintNotMetError,
    CityResidualVector,
    ResidualPoint,
    apply_city_residual_vector,
    bootstrap_city_residual_scenarios,
    compare_realized_decision,
    empirical_upper_cvar,
    optimize_scenario_aware_order,
)
from certiroute.optimization.scheduler import optimize_job_order

DEPOT = GeoPoint(latitude=33.45, longitude=-112.07)


def _profile(
    job_id: str,
    start_temperature_c: float,
    end_temperature_c: float,
) -> TemperatureProfile:
    return TemperatureProfile(
        job_id=job_id,
        points=(
            ConditionPoint(
                minute_of_day=8 * 60,
                temperature_c=start_temperature_c,
                certainty=1,
            ),
            ConditionPoint(
                minute_of_day=12 * 60,
                temperature_c=end_temperature_c,
                certainty=1,
            ),
        ),
    )


def _jobs() -> list[Job]:
    return [
        Job(
            job_id="A",
            name="Stable site",
            location=DEPOT,
            duration_minutes=30,
        ),
        Job(
            job_id="B",
            name="Fast-warming site",
            location=DEPOT,
            duration_minutes=30,
        ),
    ]


def _forecast_profiles() -> dict[str, TemperatureProfile]:
    return {
        "A": _profile("A", 24, 24),
        "B": _profile("B", 24, 42),
    }


def _early_surprise_vector(vector_id: str = "early-surprise") -> CityResidualVector:
    return CityResidualVector(
        vector_id=vector_id,
        points=(
            ResidualPoint(minute_of_day=8 * 60, residual_c=5),
            ResidualPoint(minute_of_day=9 * 60, residual_c=-5),
            ResidualPoint(minute_of_day=11 * 60, residual_c=-15),
        ),
    )


def test_city_residual_is_applied_as_one_coherent_block() -> None:
    forecasts = {
        "A": _profile("A", 20, 30),
        "B": _profile("B", 31, 35),
    }
    vector = CityResidualVector(
        vector_id="phoenix-day-1",
        points=(
            ResidualPoint(minute_of_day=8 * 60, residual_c=-2),
            ResidualPoint(minute_of_day=10 * 60, residual_c=4),
            ResidualPoint(minute_of_day=12 * 60, residual_c=1),
        ),
    )

    scenario = apply_city_residual_vector(forecasts, vector)

    for minute, expected_residual in ((8 * 60, -2), (9 * 60, 1), (10 * 60, 4)):
        errors = []
        for job_id, realized in scenario.profiles_by_job.items():
            forecast_temperature, _ = forecasts[job_id].condition_at(minute)
            scenario_temperature, _ = realized.condition_at(minute)
            errors.append(scenario_temperature - forecast_temperature)
        assert errors == pytest.approx([expected_residual, expected_residual])


def test_block_bootstrap_is_reproducible_and_samples_whole_vectors() -> None:
    forecasts = _forecast_profiles()
    vectors = [
        _early_surprise_vector("day-1"),
        CityResidualVector(
            vector_id="day-2",
            points=(
                ResidualPoint(minute_of_day=8 * 60, residual_c=-1),
                ResidualPoint(minute_of_day=11 * 60, residual_c=3),
            ),
        ),
        CityResidualVector(
            vector_id="day-3",
            points=(
                ResidualPoint(minute_of_day=8 * 60, residual_c=7),
                ResidualPoint(minute_of_day=11 * 60, residual_c=2),
            ),
        ),
    ]

    first = bootstrap_city_residual_scenarios(
        forecasts, vectors, scenario_count=8, seed=417
    )
    second = bootstrap_city_residual_scenarios(
        forecasts, vectors, scenario_count=8, seed=417
    )

    assert [scenario.source_vector_id for scenario in first] == [
        scenario.source_vector_id for scenario in second
    ]
    assert [scenario.scenario_id for scenario in first] == [
        scenario.scenario_id for scenario in second
    ]
    # Every generated scenario records exactly one source day.  It never mixes
    # a morning residual from one day with an afternoon residual from another.
    for scenario in first:
        source = next(
            vector
            for vector in vectors
            if vector.vector_id == scenario.source_vector_id
        )
        shifted = scenario.profiles_by_job["A"]
        for minute in (8 * 60, 10 * 60, 11 * 60):
            forecast, _ = forecasts["A"].condition_at(minute)
            value, _ = shifted.condition_at(minute)
            assert value - forecast == pytest.approx(source.residual_at(minute))


def test_empirical_upper_cvar_handles_a_fractional_tail() -> None:
    # At alpha=.625, the upper tail contains 1.5 equally weighted observations:
    # all of 40 and half of 20.
    assert empirical_upper_cvar([0, 10, 20, 40], alpha=0.625) == pytest.approx(100 / 3)
    assert empirical_upper_cvar([0, 10, 20, 40], alpha=0) == pytest.approx(17.5)


def test_scenario_decision_can_differ_and_realization_confirms_benefit() -> None:
    jobs = _jobs()
    forecasts = _forecast_profiles()
    ordinary = optimize_job_order(
        jobs,
        forecasts,
        strategy=ScheduleStrategy.HEAT_AWARE,
        depot=DEPOT,
        shift_start=time(8),
        shift_end=time(17),
        average_travel_speed_kph=25,
        reference_temperature_c=27,
        planning_threshold_c=28,
        uncertainty_penalty=0,
        heat_weight=10,
        priority_weight=0,
        beam_width=10,
    )
    ordinary_order = tuple(stop.job_id for stop in ordinary.stops)
    scenario = apply_city_residual_vector(
        forecasts, _early_surprise_vector(), scenario_id="held-out-realization"
    )

    robust = optimize_scenario_aware_order(
        jobs,
        forecasts,
        [scenario],
        depot=DEPOT,
        reference_temperature_c=27,
        planning_threshold_c=28,
        heat_weight=10,
        priority_weight=0,
        cvar_alpha=0.5,
        cvar_weight=1,
        chance_constraint=ChanceConstraint(
            max_minutes_at_or_above_threshold=6,
            minimum_probability=1,
        ),
    )

    assert ordinary_order == ("B", "A")
    assert robust.recommended.order == ("A", "B")
    assert robust.recommended.chance_compliance_probability == 1
    assert robust.recommended.chance_constraint_satisfied is True

    comparison = compare_realized_decision(
        robust.recommended.order,
        ordinary_order,
        jobs,
        scenario.profiles_by_job,
        depot=DEPOT,
        reference_temperature_c=27,
        planning_threshold_c=28,
        heat_weight=10,
        priority_weight=0,
    )
    assert comparison.recommended_reduced_exposure is True
    assert comparison.exposure_units_avoided > 0
    assert comparison.threshold_minutes_avoided > 0


def test_impossible_chance_constraint_is_explicit() -> None:
    forecasts = _forecast_profiles()
    scenario = apply_city_residual_vector(forecasts, _early_surprise_vector())

    with pytest.raises(ChanceConstraintNotMetError, match="best compliance"):
        optimize_scenario_aware_order(
            _jobs(),
            forecasts,
            [scenario],
            depot=DEPOT,
            reference_temperature_c=27,
            planning_threshold_c=28,
            heat_weight=10,
            priority_weight=0,
            chance_constraint=ChanceConstraint(
                max_minutes_at_or_above_threshold=0,
                minimum_probability=1,
            ),
        )


def test_nine_job_search_is_bounded() -> None:
    jobs = [
        Job(
            job_id=f"J{index}",
            name=f"Job {index}",
            location=DEPOT,
            duration_minutes=15,
        )
        for index in range(9)
    ]
    forecasts = {job.job_id: _profile(job.job_id, 30, 34) for job in jobs}
    vector = CityResidualVector(
        vector_id="neutral",
        points=(ResidualPoint(minute_of_day=8 * 60, residual_c=0),),
    )
    scenario = apply_city_residual_vector(forecasts, vector)

    result = optimize_scenario_aware_order(
        jobs,
        forecasts,
        [scenario],
        depot=DEPOT,
        max_candidate_orders=20,
        scenario_seed_limit=0,
        beam_width=20,
    )

    assert result.search_is_exhaustive is False
    assert len(result.evaluated_routes) == 20
