"""Coherent residual scenarios and risk-aware route selection.

This module turns calibrated, whole-city residual trajectories into temperature
scenarios.  A trajectory is always sampled and applied as one block: every job
in a scenario receives the same time-varying city residual.  That preserves the
shared weather error across sites and avoids the physically implausible pattern
created by independent job-hour draws.

The optimizer is intentionally separate from the deterministic scheduler.  It
uses that scheduler to evaluate feasible fixed orders, then ranks the orders by
a mean/CVaR exposure objective and, optionally, an empirical chance constraint.
For small manifests every order is considered; larger manifests use a bounded,
deterministic candidate neighbourhood so two-to-nine-job runs remain practical.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import time
from itertools import permutations
from math import ceil, factorial, isfinite
from random import Random

from pydantic import BaseModel, ConfigDict, Field, model_validator

from certiroute.domain import GeoPoint, Job
from certiroute.optimization.models import (
    ConditionPoint,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
)
from certiroute.optimization.scheduler import evaluate_job_order, optimize_job_order


class ChanceConstraintNotMetError(RuntimeError):
    """No evaluated route met the requested empirical chance constraint."""


class ResidualPoint(BaseModel):
    """One knot in a realization-minus-forecast residual trajectory."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    minute_of_day: int = Field(ge=0, lt=24 * 60)
    residual_c: float


class CityResidualVector(BaseModel):
    """One observed day's coherent, city-wide forecast-error trajectory.

    Vectors should be assembled from one city, forecast issue, horizon family,
    and realization day.  Sampling a complete vector is a block bootstrap: the
    residual at 09:00 remains paired with the residual at 15:00, and the same
    weather factor is applied to every job location.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    vector_id: str = Field(min_length=1)
    points: tuple[ResidualPoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_points(self) -> CityResidualVector:
        minutes = [point.minute_of_day for point in self.points]
        if minutes != sorted(minutes):
            raise ValueError("residual points must be ordered by minute_of_day")
        if len(minutes) != len(set(minutes)):
            raise ValueError("residual points cannot contain duplicate minutes")
        return self

    def residual_at(self, minute_of_day: float) -> float:
        """Return a linearly interpolated residual, clamped at the endpoints."""

        if minute_of_day <= self.points[0].minute_of_day:
            return self.points[0].residual_c
        if minute_of_day >= self.points[-1].minute_of_day:
            return self.points[-1].residual_c
        for left, right in zip(self.points, self.points[1:], strict=False):
            if left.minute_of_day <= minute_of_day <= right.minute_of_day:
                width = right.minute_of_day - left.minute_of_day
                fraction = (minute_of_day - left.minute_of_day) / width
                return left.residual_c + fraction * (right.residual_c - left.residual_c)
        raise RuntimeError("residual interpolation failed")


class CoherentTemperatureScenario(BaseModel):
    """Forecast profiles shifted by one whole residual vector."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    scenario_id: str = Field(min_length=1)
    source_vector_id: str = Field(min_length=1)
    profiles: tuple[TemperatureProfile, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_profiles(self) -> CoherentTemperatureScenario:
        identifiers = [profile.job_id for profile in self.profiles]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("scenario profiles must have unique job IDs")
        return self

    @property
    def profiles_by_job(self) -> dict[str, TemperatureProfile]:
        """Return the profiles in the mapping format expected by the scheduler."""

        return {profile.job_id: profile for profile in self.profiles}


class ChanceConstraint(BaseModel):
    """Empirical requirement on route minutes at or above a heat threshold."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    max_minutes_at_or_above_threshold: float = Field(ge=0)
    minimum_probability: float = Field(default=0.9, ge=0, le=1)


class ScenarioPlanEvaluation(BaseModel):
    """One fixed route evaluated under one coherent temperature scenario."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    scenario_id: str
    source_vector_id: str
    plan: SchedulePlan
    exposure_units: float = Field(ge=0)
    threshold_minutes: float = Field(ge=0)
    chance_event_satisfied: bool | None = None


class ScenarioRouteScore(BaseModel):
    """Distribution-aware score for one candidate job order."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    order: tuple[str, ...] = Field(min_length=1)
    nominal_plan: SchedulePlan
    scenario_evaluations: tuple[ScenarioPlanEvaluation, ...] = Field(min_length=1)
    expected_exposure_units: float = Field(ge=0)
    value_at_risk_units: float = Field(ge=0)
    cvar_exposure_units: float = Field(ge=0)
    risk_adjusted_exposure_units: float = Field(ge=0)
    expected_threshold_minutes: float = Field(ge=0)
    chance_compliance_probability: float | None = Field(default=None, ge=0, le=1)
    chance_constraint_satisfied: bool | None = None
    objective_value: float = Field(ge=0)


class ScenarioOptimizationResult(BaseModel):
    """Winning robust route and the alternatives considered beside it."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    recommended: ScenarioRouteScore
    evaluated_routes: tuple[ScenarioRouteScore, ...] = Field(min_length=1)
    scenario_count: int = Field(gt=0)
    cvar_alpha: float = Field(ge=0, lt=1)
    cvar_weight: float = Field(ge=0, le=1)
    search_is_exhaustive: bool


class RealizedRouteEvaluation(BaseModel):
    """A previously selected order scored against later realized profiles."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    order: tuple[str, ...] = Field(min_length=1)
    plan: SchedulePlan
    exposure_units: float = Field(ge=0)
    threshold_minutes: float = Field(ge=0)


class RealizedDecisionComparison(BaseModel):
    """Outcome of the robust decision versus an ordinary comparator route."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    recommended: RealizedRouteEvaluation
    comparator: RealizedRouteEvaluation
    exposure_units_avoided: float
    threshold_minutes_avoided: float
    travel_minutes_difference: int
    recommended_reduced_exposure: bool


def apply_city_residual_vector(
    forecast_profiles: Mapping[str, TemperatureProfile],
    residual_vector: CityResidualVector,
    *,
    scenario_id: str | None = None,
) -> CoherentTemperatureScenario:
    """Apply one time-coherent residual trajectory to every forecast profile."""

    _validate_profile_mapping(forecast_profiles)
    shifted_profiles = []
    residual_minutes = {point.minute_of_day for point in residual_vector.points}
    for job_id in sorted(forecast_profiles):
        forecast = forecast_profiles[job_id]
        minutes = {point.minute_of_day for point in forecast.points}
        # Keep every residual knot.  Outside the forecast's explicit range the
        # existing profile semantics clamp its endpoint, while the observed
        # residual trajectory is still allowed to evolve across the shift.
        minutes.update(residual_minutes)
        points = []
        for minute in sorted(minutes):
            temperature, certainty = forecast.condition_at(minute)
            points.append(
                ConditionPoint(
                    minute_of_day=minute,
                    temperature_c=(temperature + residual_vector.residual_at(minute)),
                    certainty=certainty,
                )
            )
        shifted_profiles.append(TemperatureProfile(job_id=job_id, points=tuple(points)))
    return CoherentTemperatureScenario(
        scenario_id=scenario_id or residual_vector.vector_id,
        source_vector_id=residual_vector.vector_id,
        profiles=tuple(shifted_profiles),
    )


def bootstrap_city_residual_scenarios(
    forecast_profiles: Mapping[str, TemperatureProfile],
    residual_vectors: Sequence[CityResidualVector],
    *,
    scenario_count: int,
    seed: int = 0,
) -> tuple[CoherentTemperatureScenario, ...]:
    """Block-bootstrap complete city/day residual vectors with a local RNG."""

    if not residual_vectors:
        raise ValueError("at least one residual vector is required")
    if scenario_count < 1:
        raise ValueError("scenario_count must be at least one")
    _validate_profile_mapping(forecast_profiles)
    random = Random(seed)
    scenarios = []
    for index in range(scenario_count):
        vector = residual_vectors[random.randrange(len(residual_vectors))]
        scenarios.append(
            apply_city_residual_vector(
                forecast_profiles,
                vector,
                scenario_id=f"scenario-{index + 1:04d}:{vector.vector_id}",
            )
        )
    return tuple(scenarios)


def empirical_upper_cvar(values: Sequence[float], *, alpha: float = 0.9) -> float:
    """Return equally weighted upper-tail CVaR, including fractional samples."""

    if not values:
        raise ValueError("CVaR requires at least one value")
    if not 0 <= alpha < 1:
        raise ValueError("alpha must be in [0, 1)")
    if any(not isfinite(value) or value < 0 for value in values):
        raise ValueError("CVaR exposure values must be finite and non-negative")

    descending = sorted(values, reverse=True)
    tail_mass = (1 - alpha) * len(descending)
    full_samples = int(tail_mass)
    fractional_sample = tail_mass - full_samples
    tail_sum = sum(descending[:full_samples])
    if fractional_sample:
        tail_sum += fractional_sample * descending[full_samples]
    return tail_sum / tail_mass


def evaluate_order_across_scenarios(
    order: Sequence[str],
    jobs: Sequence[Job],
    forecast_profiles: Mapping[str, TemperatureProfile],
    scenarios: Sequence[CoherentTemperatureScenario],
    *,
    depot: GeoPoint,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    heat_weight: float = 4.0,
    priority_weight: float = 0.2,
    cvar_alpha: float = 0.9,
    cvar_weight: float = 0.5,
    chance_constraint: ChanceConstraint | None = None,
) -> ScenarioRouteScore:
    """Evaluate a fixed order under every coherent temperature scenario."""

    if not scenarios:
        raise ValueError("at least one scenario is required")
    if not 0 <= cvar_alpha < 1:
        raise ValueError("cvar_alpha must be in [0, 1)")
    if not 0 <= cvar_weight <= 1:
        raise ValueError("cvar_weight must be in [0, 1]")
    if heat_weight < 0:
        raise ValueError("heat_weight cannot be negative")
    if priority_weight < 0:
        raise ValueError("priority_weight cannot be negative")

    ordered_jobs = _jobs_in_order(jobs, order)
    _validate_scenario_coverage(ordered_jobs, forecast_profiles, scenarios)
    common = {
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": average_travel_speed_kph,
        "reference_temperature_c": reference_temperature_c,
        "planning_threshold_c": planning_threshold_c,
        "uncertainty_penalty": 0.0,
        "heat_weight": heat_weight,
        "priority_weight": priority_weight,
    }
    nominal_plan = evaluate_job_order(
        ordered_jobs,
        dict(forecast_profiles),
        strategy=ScheduleStrategy.HEAT_AWARE,
        **common,
    )
    evaluations = []
    exposures = []
    threshold_minutes = []
    chance_hits = 0
    for scenario in scenarios:
        scenario_profiles = scenario.profiles_by_job
        plan = evaluate_job_order(
            ordered_jobs,
            scenario_profiles,
            strategy=ScheduleStrategy.HEAT_AWARE,
            **common,
        )
        exposure, minutes = _exact_route_risk(
            plan,
            scenario_profiles,
            reference_temperature_c=reference_temperature_c,
            planning_threshold_c=planning_threshold_c,
        )
        event = None
        if chance_constraint is not None:
            event = minutes <= chance_constraint.max_minutes_at_or_above_threshold
            chance_hits += int(event)
        evaluations.append(
            ScenarioPlanEvaluation(
                scenario_id=scenario.scenario_id,
                source_vector_id=scenario.source_vector_id,
                plan=plan,
                exposure_units=exposure,
                threshold_minutes=minutes,
                chance_event_satisfied=event,
            )
        )
        exposures.append(exposure)
        threshold_minutes.append(minutes)

    expected_exposure = sum(exposures) / len(exposures)
    cvar = empirical_upper_cvar(exposures, alpha=cvar_alpha)
    risk_adjusted = (1 - cvar_weight) * expected_exposure + cvar_weight * cvar
    ascending = sorted(exposures)
    var_index = max(0, ceil(cvar_alpha * len(ascending)) - 1)
    probability = None
    constraint_satisfied = None
    if chance_constraint is not None:
        probability = chance_hits / len(scenarios)
        constraint_satisfied = probability >= chance_constraint.minimum_probability
    objective = (
        nominal_plan.total_travel_minutes
        + heat_weight * risk_adjusted
        + priority_weight * nominal_plan.priority_weighted_delay_minutes
    )
    return ScenarioRouteScore(
        order=tuple(order),
        nominal_plan=nominal_plan,
        scenario_evaluations=tuple(evaluations),
        expected_exposure_units=round(expected_exposure, 6),
        value_at_risk_units=round(ascending[var_index], 6),
        cvar_exposure_units=round(cvar, 6),
        risk_adjusted_exposure_units=round(risk_adjusted, 6),
        expected_threshold_minutes=round(
            sum(threshold_minutes) / len(threshold_minutes), 6
        ),
        chance_compliance_probability=(
            round(probability, 6) if probability is not None else None
        ),
        chance_constraint_satisfied=constraint_satisfied,
        objective_value=round(objective, 6),
    )


def optimize_scenario_aware_order(
    jobs: Sequence[Job],
    forecast_profiles: Mapping[str, TemperatureProfile],
    scenarios: Sequence[CoherentTemperatureScenario],
    *,
    depot: GeoPoint,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    heat_weight: float = 4.0,
    priority_weight: float = 0.2,
    cvar_alpha: float = 0.9,
    cvar_weight: float = 0.5,
    chance_constraint: ChanceConstraint | None = None,
    max_candidate_orders: int = 512,
    scenario_seed_limit: int = 12,
    beam_width: int = 1000,
) -> ScenarioOptimizationResult:
    """Select a feasible order using coherent scenario risk.

    Up to five jobs are exhaustively searched with the default candidate cap.
    For larger manifests, nominal/scenario optima seed a deterministic pair-swap
    neighbourhood capped by ``max_candidate_orders``.
    """

    if not 2 <= len(jobs) <= 9:
        raise ValueError("scenario-aware optimization supports 2 to 9 jobs")
    if max_candidate_orders < 2:
        raise ValueError("max_candidate_orders must be at least two")
    if scenario_seed_limit < 0:
        raise ValueError("scenario_seed_limit cannot be negative")
    if beam_width < 1:
        raise ValueError("beam_width must be at least one")
    _validate_scenario_coverage(jobs, forecast_profiles, scenarios)

    common = {
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": average_travel_speed_kph,
        "reference_temperature_c": reference_temperature_c,
        "planning_threshold_c": planning_threshold_c,
        "uncertainty_penalty": 0.0,
        "heat_weight": heat_weight,
        "priority_weight": priority_weight,
        "beam_width": beam_width,
    }
    nominal = optimize_job_order(
        list(jobs),
        dict(forecast_profiles),
        strategy=ScheduleStrategy.HEAT_AWARE,
        **common,
    )
    seed_orders = [
        tuple(job.job_id for job in jobs),
        tuple(stop.job_id for stop in nominal.stops),
    ]
    seen_vectors = set()
    for scenario in scenarios:
        if len(seen_vectors) >= scenario_seed_limit:
            break
        if scenario.source_vector_id in seen_vectors:
            continue
        seen_vectors.add(scenario.source_vector_id)
        scenario_plan = optimize_job_order(
            list(jobs),
            scenario.profiles_by_job,
            strategy=ScheduleStrategy.HEAT_AWARE,
            **common,
        )
        seed_orders.append(tuple(stop.job_id for stop in scenario_plan.stops))

    candidate_orders, exhaustive = _candidate_orders(
        jobs,
        seed_orders=seed_orders,
        max_candidate_orders=max_candidate_orders,
    )
    score_common = {
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": average_travel_speed_kph,
        "reference_temperature_c": reference_temperature_c,
        "planning_threshold_c": planning_threshold_c,
        "heat_weight": heat_weight,
        "priority_weight": priority_weight,
        "cvar_alpha": cvar_alpha,
        "cvar_weight": cvar_weight,
        "chance_constraint": chance_constraint,
    }
    scores = tuple(
        evaluate_order_across_scenarios(
            order,
            jobs,
            forecast_profiles,
            scenarios,
            **score_common,
        )
        for order in candidate_orders
    )
    eligible = (
        tuple(score for score in scores if score.chance_constraint_satisfied)
        if chance_constraint is not None
        else scores
    )
    if not eligible:
        best_probability = max(
            score.chance_compliance_probability or 0.0 for score in scores
        )
        raise ChanceConstraintNotMetError(
            "No evaluated route met the empirical chance constraint; "
            f"best compliance probability was {best_probability:.3f}"
        )
    recommended = min(
        eligible,
        key=lambda score: (
            score.objective_value,
            score.cvar_exposure_units,
            score.nominal_plan.route_finish_minute,
            score.order,
        ),
    )
    return ScenarioOptimizationResult(
        recommended=recommended,
        evaluated_routes=scores,
        scenario_count=len(scenarios),
        cvar_alpha=cvar_alpha,
        cvar_weight=cvar_weight,
        search_is_exhaustive=exhaustive,
    )


def evaluate_realized_order(
    order: Sequence[str],
    jobs: Sequence[Job],
    realized_profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    heat_weight: float = 4.0,
    priority_weight: float = 0.2,
) -> RealizedRouteEvaluation:
    """Backtest a chosen order without re-optimizing after reality is known."""

    ordered_jobs = _jobs_in_order(jobs, order)
    plan = evaluate_job_order(
        ordered_jobs,
        dict(realized_profiles),
        strategy=ScheduleStrategy.HEAT_AWARE,
        depot=depot,
        shift_start=shift_start,
        shift_end=shift_end,
        average_travel_speed_kph=average_travel_speed_kph,
        reference_temperature_c=reference_temperature_c,
        planning_threshold_c=planning_threshold_c,
        uncertainty_penalty=0.0,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
    )
    exposure, minutes = _exact_route_risk(
        plan,
        realized_profiles,
        reference_temperature_c=reference_temperature_c,
        planning_threshold_c=planning_threshold_c,
    )
    return RealizedRouteEvaluation(
        order=tuple(order),
        plan=plan,
        exposure_units=exposure,
        threshold_minutes=minutes,
    )


def compare_realized_decision(
    recommended_order: Sequence[str],
    comparator_order: Sequence[str],
    jobs: Sequence[Job],
    realized_profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    heat_weight: float = 4.0,
    priority_weight: float = 0.2,
) -> RealizedDecisionComparison:
    """Compare a frozen robust decision with a frozen ordinary route."""

    common = {
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": average_travel_speed_kph,
        "reference_temperature_c": reference_temperature_c,
        "planning_threshold_c": planning_threshold_c,
        "heat_weight": heat_weight,
        "priority_weight": priority_weight,
    }
    recommended = evaluate_realized_order(
        recommended_order, jobs, realized_profiles, **common
    )
    comparator = evaluate_realized_order(
        comparator_order, jobs, realized_profiles, **common
    )
    exposure_avoided = comparator.exposure_units - recommended.exposure_units
    threshold_avoided = comparator.threshold_minutes - recommended.threshold_minutes
    return RealizedDecisionComparison(
        recommended=recommended,
        comparator=comparator,
        exposure_units_avoided=round(exposure_avoided, 6),
        threshold_minutes_avoided=round(threshold_avoided, 6),
        travel_minutes_difference=(
            recommended.plan.total_travel_minutes - comparator.plan.total_travel_minutes
        ),
        recommended_reduced_exposure=exposure_avoided > 0,
    )


def _candidate_orders(
    jobs: Sequence[Job],
    *,
    seed_orders: Sequence[tuple[str, ...]],
    max_candidate_orders: int,
) -> tuple[tuple[tuple[str, ...], ...], bool]:
    identifiers = tuple(job.job_id for job in jobs)
    permutation_count = factorial(len(identifiers))
    if permutation_count <= max_candidate_orders:
        return tuple(permutations(identifiers)), True

    candidates: list[tuple[str, ...]] = []
    seen: set[tuple[str, ...]] = set()

    def add(order: tuple[str, ...]) -> None:
        if len(candidates) < max_candidate_orders and order not in seen:
            candidates.append(order)
            seen.add(order)

    for order in seed_orders:
        add(order)
    for seed in tuple(candidates):
        for left in range(len(seed) - 1):
            for right in range(left + 1, len(seed)):
                swapped = list(seed)
                swapped[left], swapped[right] = swapped[right], swapped[left]
                add(tuple(swapped))
                if len(candidates) >= max_candidate_orders:
                    return tuple(candidates), False
    return tuple(candidates), False


def _exact_route_risk(
    plan: SchedulePlan,
    profiles: Mapping[str, TemperatureProfile],
    *,
    reference_temperature_c: float,
    planning_threshold_c: float,
) -> tuple[float, float]:
    """Recompute risk before presentation rounding in ``SchedulePlan``."""

    exposure = 0.0
    threshold_minutes = 0.0
    for stop in plan.stops:
        profile = profiles[stop.job_id]
        exposure += profile.degree_hours_above(
            reference_temperature_c,
            stop.start_minute,
            stop.finish_minute,
        )
        threshold_minutes += profile.minutes_at_or_above(
            planning_threshold_c,
            stop.start_minute,
            stop.finish_minute,
        )
    return exposure, threshold_minutes


def _jobs_in_order(jobs: Sequence[Job], order: Sequence[str]) -> list[Job]:
    jobs_by_id = {job.job_id: job for job in jobs}
    if len(jobs_by_id) != len(jobs):
        raise ValueError("job IDs must be unique")
    order_tuple = tuple(order)
    if len(order_tuple) != len(set(order_tuple)):
        raise ValueError("order cannot contain duplicate job IDs")
    if set(order_tuple) != set(jobs_by_id):
        raise ValueError("order must contain every job ID exactly once")
    return [jobs_by_id[job_id] for job_id in order_tuple]


def _validate_profile_mapping(
    profiles: Mapping[str, TemperatureProfile],
) -> None:
    if not profiles:
        raise ValueError("at least one forecast profile is required")
    mismatches = sorted(
        key for key, profile in profiles.items() if key != profile.job_id
    )
    if mismatches:
        raise ValueError(
            "profile mapping keys must match profile job IDs: " + ", ".join(mismatches)
        )


def _validate_scenario_coverage(
    jobs: Sequence[Job],
    forecast_profiles: Mapping[str, TemperatureProfile],
    scenarios: Sequence[CoherentTemperatureScenario],
) -> None:
    if not scenarios:
        raise ValueError("at least one scenario is required")
    _validate_profile_mapping(forecast_profiles)
    job_ids = [job.job_id for job in jobs]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("job IDs must be unique")
    scenario_ids = [scenario.scenario_id for scenario in scenarios]
    if len(scenario_ids) != len(set(scenario_ids)):
        raise ValueError("scenario IDs must be unique")
    expected = {job.job_id for job in jobs}
    if set(forecast_profiles) != expected:
        raise ValueError("forecast profiles must exactly cover the jobs")
    for scenario in scenarios:
        if set(scenario.profiles_by_job) != expected:
            raise ValueError(
                f"scenario {scenario.scenario_id!r} must exactly cover the jobs"
            )
