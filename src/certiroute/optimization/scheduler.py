"""Transparent beam-search scheduler for the CertiRoute hackathon MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from math import asin, ceil, cos, radians, sin, sqrt

from certiroute.domain import GeoPoint, Job
from certiroute.optimization.models import (
    ScheduledStop,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
)
from certiroute.risk import estimate_ambient_exposure


class InfeasibleScheduleError(RuntimeError):
    """No schedule satisfied every configured time constraint."""


@dataclass(frozen=True)
class _Candidate:
    order: tuple[str, ...]
    stops: tuple[ScheduledStop, ...]
    last_location: GeoPoint
    current_minute: int
    travel_minutes: int
    raw_exposure_units: float
    adjusted_exposure_units: float
    threshold_minutes: int


def compare_schedules(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    uncertainty_penalty: float = 0.5,
    heat_weight: float = 4.0,
    beam_width: int = 5000,
) -> dict[ScheduleStrategy, SchedulePlan]:
    """Build the original plan and three directly comparable alternatives."""

    common = {
        "profiles": profiles,
        "depot": depot,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "average_travel_speed_kph": average_travel_speed_kph,
        "reference_temperature_c": reference_temperature_c,
        "planning_threshold_c": planning_threshold_c,
        "uncertainty_penalty": uncertainty_penalty,
        "heat_weight": heat_weight,
    }
    plans = {
        ScheduleStrategy.ORIGINAL: evaluate_job_order(
            jobs, strategy=ScheduleStrategy.ORIGINAL, **common
        )
    }
    for strategy in (
        ScheduleStrategy.EFFICIENCY,
        ScheduleStrategy.HEAT_AWARE,
        ScheduleStrategy.CERTAINTY_AWARE,
    ):
        plans[strategy] = optimize_job_order(
            jobs,
            strategy=strategy,
            beam_width=beam_width,
            **common,
        )
    return plans


def evaluate_job_order(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    strategy: ScheduleStrategy,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
    average_travel_speed_kph: float,
    reference_temperature_c: float,
    planning_threshold_c: float,
    uncertainty_penalty: float,
    heat_weight: float,
) -> SchedulePlan:
    """Evaluate one fixed order using the same constraints as optimization."""

    _validate_inputs(
        jobs,
        profiles,
        average_travel_speed_kph=average_travel_speed_kph,
        heat_weight=heat_weight,
    )
    candidate = _initial_candidate(depot, shift_start)
    for job in jobs:
        candidate = _extend_candidate(
            candidate,
            job,
            profiles[job.job_id],
            shift_start=shift_start,
            shift_end=shift_end,
            average_travel_speed_kph=average_travel_speed_kph,
            reference_temperature_c=reference_temperature_c,
            planning_threshold_c=planning_threshold_c,
            uncertainty_penalty=uncertainty_penalty,
        )
        if candidate is None:
            raise InfeasibleScheduleError(
                f"Fixed order becomes infeasible at job {job.job_id}"
            )
    return _finalize_candidate(
        candidate,
        strategy=strategy,
        depot=depot,
        shift_end=shift_end,
        average_travel_speed_kph=average_travel_speed_kph,
        heat_weight=heat_weight,
    )


def optimize_job_order(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    strategy: ScheduleStrategy,
    depot: GeoPoint,
    shift_start: time,
    shift_end: time,
    average_travel_speed_kph: float,
    reference_temperature_c: float,
    planning_threshold_c: float,
    uncertainty_penalty: float,
    heat_weight: float,
    beam_width: int = 5000,
) -> SchedulePlan:
    """Find a strong feasible order while bounding combinatorial growth."""

    if strategy is ScheduleStrategy.ORIGINAL:
        raise ValueError("use evaluate_job_order for the original strategy")
    if beam_width < 1:
        raise ValueError("beam_width must be at least one")
    _validate_inputs(
        jobs,
        profiles,
        average_travel_speed_kph=average_travel_speed_kph,
        heat_weight=heat_weight,
    )

    jobs_by_id = {job.job_id: job for job in jobs}
    candidates = [_initial_candidate(depot, shift_start)]
    for _ in jobs:
        expanded: list[_Candidate] = []
        for candidate in candidates:
            remaining = jobs_by_id.keys() - candidate.order
            for job_id in remaining:
                job = jobs_by_id[job_id]
                next_candidate = _extend_candidate(
                    candidate,
                    job,
                    profiles[job_id],
                    shift_start=shift_start,
                    shift_end=shift_end,
                    average_travel_speed_kph=average_travel_speed_kph,
                    reference_temperature_c=reference_temperature_c,
                    planning_threshold_c=planning_threshold_c,
                    uncertainty_penalty=uncertainty_penalty,
                )
                if next_candidate is not None:
                    expanded.append(next_candidate)
        if not expanded:
            raise InfeasibleScheduleError(
                "No job ordering satisfies all job and shift time windows"
            )
        expanded.sort(
            key=lambda candidate: (
                _candidate_score(candidate, strategy, heat_weight),
                candidate.current_minute,
                candidate.order,
            )
        )
        candidates = expanded[:beam_width]

    finalized: list[SchedulePlan] = []
    for candidate in candidates:
        try:
            finalized.append(
                _finalize_candidate(
                    candidate,
                    strategy=strategy,
                    depot=depot,
                    shift_end=shift_end,
                    average_travel_speed_kph=average_travel_speed_kph,
                    heat_weight=heat_weight,
                )
            )
        except InfeasibleScheduleError:
            continue
    if not finalized:
        raise InfeasibleScheduleError(
            "No route can return to the depot within the shift"
        )
    return min(
        finalized,
        key=lambda plan: (plan.objective_value, plan.route_finish_minute),
    )


def _initial_candidate(depot: GeoPoint, shift_start: time) -> _Candidate:
    return _Candidate(
        order=(),
        stops=(),
        last_location=depot,
        current_minute=_time_to_minute(shift_start),
        travel_minutes=0,
        raw_exposure_units=0.0,
        adjusted_exposure_units=0.0,
        threshold_minutes=0,
    )


def _extend_candidate(
    candidate: _Candidate,
    job: Job,
    profile: TemperatureProfile,
    *,
    shift_start: time,
    shift_end: time,
    average_travel_speed_kph: float,
    reference_temperature_c: float,
    planning_threshold_c: float,
    uncertainty_penalty: float,
) -> _Candidate | None:
    travel = _travel_minutes(
        candidate.last_location,
        job.location,
        average_travel_speed_kph=average_travel_speed_kph,
    )
    arrival = candidate.current_minute + travel
    earliest = max(
        _time_to_minute(shift_start),
        _time_to_minute(job.earliest_start) if job.earliest_start else 0,
    )
    latest = min(
        _time_to_minute(shift_end),
        _time_to_minute(job.latest_finish) if job.latest_finish else 24 * 60,
    )
    start = max(arrival, earliest)
    finish = start + job.duration_minutes
    if finish > latest:
        return None

    midpoint = start + job.duration_minutes / 2
    temperature, certainty = profile.condition_at(midpoint)
    exposure = estimate_ambient_exposure(
        temperature_c=temperature,
        duration_minutes=job.duration_minutes,
        certainty=certainty,
        reference_temperature_c=reference_temperature_c,
        uncertainty_penalty=uncertainty_penalty,
    )
    stop = ScheduledStop(
        sequence=len(candidate.stops) + 1,
        job_id=job.job_id,
        job_name=job.name,
        latitude=job.location.latitude,
        longitude=job.location.longitude,
        arrival_minute=arrival,
        start_minute=start,
        finish_minute=finish,
        inbound_travel_minutes=travel,
        temperature_c=round(temperature, 2),
        certainty=round(certainty, 4),
        raw_exposure_units=exposure.raw_exposure_units,
        certainty_adjusted_units=exposure.certainty_adjusted_units,
    )
    return _Candidate(
        order=(*candidate.order, job.job_id),
        stops=(*candidate.stops, stop),
        last_location=job.location,
        current_minute=finish,
        travel_minutes=candidate.travel_minutes + travel,
        raw_exposure_units=(candidate.raw_exposure_units + exposure.raw_exposure_units),
        adjusted_exposure_units=(
            candidate.adjusted_exposure_units + exposure.certainty_adjusted_units
        ),
        threshold_minutes=(
            candidate.threshold_minutes
            + (job.duration_minutes if temperature >= planning_threshold_c else 0)
        ),
    )


def _finalize_candidate(
    candidate: _Candidate,
    *,
    strategy: ScheduleStrategy,
    depot: GeoPoint,
    shift_end: time,
    average_travel_speed_kph: float,
    heat_weight: float,
) -> SchedulePlan:
    return_travel = _travel_minutes(
        candidate.last_location,
        depot,
        average_travel_speed_kph=average_travel_speed_kph,
    )
    finish = candidate.current_minute + return_travel
    if finish > _time_to_minute(shift_end):
        raise InfeasibleScheduleError("route cannot return to depot within the shift")
    travel = candidate.travel_minutes + return_travel
    objective = _score_values(
        travel_minutes=travel,
        raw_exposure_units=candidate.raw_exposure_units,
        adjusted_exposure_units=candidate.adjusted_exposure_units,
        strategy=strategy,
        heat_weight=heat_weight,
    )
    return SchedulePlan(
        strategy=strategy,
        stops=candidate.stops,
        total_travel_minutes=travel,
        total_raw_exposure_units=round(candidate.raw_exposure_units, 3),
        total_adjusted_exposure_units=round(candidate.adjusted_exposure_units, 3),
        minutes_above_planning_threshold=candidate.threshold_minutes,
        route_finish_minute=finish,
        objective_value=round(objective, 3),
    )


def _candidate_score(
    candidate: _Candidate, strategy: ScheduleStrategy, heat_weight: float
) -> float:
    return _score_values(
        travel_minutes=candidate.travel_minutes,
        raw_exposure_units=candidate.raw_exposure_units,
        adjusted_exposure_units=candidate.adjusted_exposure_units,
        strategy=strategy,
        heat_weight=heat_weight,
    )


def _score_values(
    *,
    travel_minutes: int,
    raw_exposure_units: float,
    adjusted_exposure_units: float,
    strategy: ScheduleStrategy,
    heat_weight: float,
) -> float:
    if strategy in (ScheduleStrategy.ORIGINAL, ScheduleStrategy.EFFICIENCY):
        return float(travel_minutes)
    if strategy is ScheduleStrategy.HEAT_AWARE:
        return travel_minutes + heat_weight * raw_exposure_units
    return travel_minutes + heat_weight * adjusted_exposure_units


def _validate_inputs(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    average_travel_speed_kph: float,
    heat_weight: float,
) -> None:
    if not jobs:
        raise ValueError("at least one job is required")
    job_ids = [job.job_id for job in jobs]
    if len(job_ids) != len(set(job_ids)):
        raise ValueError("job IDs must be unique")
    missing_profiles = set(job_ids) - profiles.keys()
    if missing_profiles:
        missing = ", ".join(sorted(missing_profiles))
        raise ValueError(f"missing temperature profiles for: {missing}")
    if average_travel_speed_kph <= 0:
        raise ValueError("average_travel_speed_kph must be greater than zero")
    if heat_weight < 0:
        raise ValueError("heat_weight cannot be negative")


def _travel_minutes(
    start: GeoPoint,
    end: GeoPoint,
    *,
    average_travel_speed_kph: float,
) -> int:
    if start == end:
        return 0
    distance_km = _haversine_km(start, end)
    return max(1, ceil(distance_km / average_travel_speed_kph * 60))


def _haversine_km(start: GeoPoint, end: GeoPoint) -> float:
    earth_radius_km = 6371.0088
    lat1 = radians(start.latitude)
    lat2 = radians(end.latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(end.longitude - start.longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(value))


def _time_to_minute(value: time) -> int:
    return value.hour * 60 + value.minute
