"""Transparent beam-search scheduler for the CertiRoute hackathon MVP."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import time
from functools import lru_cache
from math import asin, ceil, cos, radians, sin, sqrt
from typing import NamedTuple

from certiroute.domain import GeoPoint, Job
from certiroute.optimization.models import (
    IntervalRiskSummary,
    ScheduledStop,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
    interval_risk_summary,
)


class InfeasibleScheduleError(RuntimeError):
    """No schedule satisfied every configured time constraint."""


class ScheduleSearchLimitError(RuntimeError):
    """A bounded search found no plan after pruning candidate branches."""


class _StopDraft(NamedTuple):
    """Unrounded per-stop data; ScheduledStop models are built only for the
    winning candidate because beam search discards almost every candidate."""

    job_id: str
    job_name: str
    latitude: float
    longitude: float
    arrival_minute: int
    start_minute: int
    finish_minute: int
    inbound_travel_minutes: int
    conditions: IntervalRiskSummary


@dataclass(frozen=True)
class _Candidate:
    order: tuple[str, ...]
    stops: tuple[_StopDraft, ...]
    last_location: GeoPoint
    current_minute: int
    travel_minutes: int
    raw_exposure_units: float
    adjusted_exposure_units: float
    threshold_minutes: float
    priority_weighted_delay_minutes: float


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
    priority_weight: float = 0.2,
    beam_width: int = 5000,
) -> dict[ScheduleStrategy, SchedulePlan]:
    """Build four directly comparable plans containing the same jobs.

    Infeasibility is surfaced to the caller instead of silently deleting work;
    an operator-facing triage decision needs an explicit policy and approval.
    """

    _validate_inputs(
        jobs,
        profiles,
        average_travel_speed_kph=average_travel_speed_kph,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
        uncertainty_penalty=uncertainty_penalty,
    )

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
        "priority_weight": priority_weight,
    }
    plans = {
        ScheduleStrategy.ORIGINAL: evaluate_job_order(
            jobs,
            strategy=ScheduleStrategy.ORIGINAL,
            **common,
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
    priority_weight: float = 0.2,
) -> SchedulePlan:
    """Evaluate one fixed order using the same constraints as optimization."""

    _validate_inputs(
        jobs,
        profiles,
        average_travel_speed_kph=average_travel_speed_kph,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
        uncertainty_penalty=uncertainty_penalty,
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
        priority_weight=priority_weight,
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
    priority_weight: float = 0.2,
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
        priority_weight=priority_weight,
        uncertainty_penalty=uncertainty_penalty,
    )

    jobs_by_id = {job.job_id: job for job in jobs}
    candidates = [_initial_candidate(depot, shift_start)]
    search_was_truncated = False
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
            if search_was_truncated:
                raise ScheduleSearchLimitError(
                    "No feasible schedule was found within the configured beam "
                    f"width of {beam_width}"
                )
            raise InfeasibleScheduleError(
                "No job ordering satisfies all job and shift time windows"
            )
        expanded.sort(
            key=lambda candidate: (
                _candidate_score(
                    candidate,
                    strategy,
                    heat_weight=heat_weight,
                    priority_weight=priority_weight,
                ),
                candidate.current_minute,
                candidate.order,
            )
        )
        search_was_truncated = search_was_truncated or len(expanded) > beam_width
        candidates = expanded[:beam_width]

    best_key: tuple[float, int] | None = None
    best: tuple[_Candidate, int, int] | None = None
    for candidate in candidates:
        completion = _route_completion(
            candidate,
            depot=depot,
            shift_end=shift_end,
            average_travel_speed_kph=average_travel_speed_kph,
        )
        if completion is None:
            continue
        travel, finish = completion
        objective = round(
            _score_values(
                travel_minutes=travel,
                raw_exposure_units=candidate.raw_exposure_units,
                adjusted_exposure_units=candidate.adjusted_exposure_units,
                strategy=strategy,
                heat_weight=heat_weight,
                priority_weight=priority_weight,
                priority_weighted_delay_minutes=(
                    candidate.priority_weighted_delay_minutes
                ),
            ),
            3,
        )
        key = (objective, finish)
        if best_key is None or key < best_key:
            best_key = key
            best = (candidate, travel, finish)
    if best is None:
        if search_was_truncated:
            raise ScheduleSearchLimitError(
                "No depot-returning schedule was found within the configured "
                f"beam width of {beam_width}"
            )
        raise InfeasibleScheduleError(
            "No route can return to the depot within the shift"
        )
    return _build_plan(
        best[0],
        strategy=strategy,
        total_travel_minutes=best[1],
        route_finish_minute=best[2],
        heat_weight=heat_weight,
        priority_weight=priority_weight,
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
        threshold_minutes=0.0,
        priority_weighted_delay_minutes=0.0,
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

    start_f, finish_f = float(start), float(finish)
    conditions = interval_risk_summary(
        profile,
        start_f,
        finish_f,
        reference_temperature_c,
        planning_threshold_c,
        uncertainty_penalty,
    )
    priority_delay = (job.priority / 5) * (start - earliest)
    draft = _StopDraft(
        job_id=job.job_id,
        job_name=job.name,
        latitude=job.location.latitude,
        longitude=job.location.longitude,
        arrival_minute=arrival,
        start_minute=start,
        finish_minute=finish,
        inbound_travel_minutes=travel,
        conditions=conditions,
    )
    return _Candidate(
        order=(*candidate.order, job.job_id),
        stops=(*candidate.stops, draft),
        last_location=job.location,
        current_minute=finish,
        travel_minutes=candidate.travel_minutes + travel,
        raw_exposure_units=(
            candidate.raw_exposure_units + conditions.degree_hours_above_reference
        ),
        adjusted_exposure_units=(
            candidate.adjusted_exposure_units
            + conditions.certainty_adjusted_degree_hours
        ),
        threshold_minutes=(
            candidate.threshold_minutes + conditions.minutes_at_or_above_threshold
        ),
        priority_weighted_delay_minutes=(
            candidate.priority_weighted_delay_minutes + priority_delay
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
    priority_weight: float,
) -> SchedulePlan:
    completion = _route_completion(
        candidate,
        depot=depot,
        shift_end=shift_end,
        average_travel_speed_kph=average_travel_speed_kph,
    )
    if completion is None:
        raise InfeasibleScheduleError("route cannot return to depot within the shift")
    travel, finish = completion
    return _build_plan(
        candidate,
        strategy=strategy,
        total_travel_minutes=travel,
        route_finish_minute=finish,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
    )


def _route_completion(
    candidate: _Candidate,
    *,
    depot: GeoPoint,
    shift_end: time,
    average_travel_speed_kph: float,
) -> tuple[int, int] | None:
    """Return (total travel, depot-return minute), or None when out of shift."""

    return_travel = _travel_minutes(
        candidate.last_location,
        depot,
        average_travel_speed_kph=average_travel_speed_kph,
    )
    finish = candidate.current_minute + return_travel
    if finish > _time_to_minute(shift_end):
        return None
    return candidate.travel_minutes + return_travel, finish


def _build_plan(
    candidate: _Candidate,
    *,
    strategy: ScheduleStrategy,
    total_travel_minutes: int,
    route_finish_minute: int,
    heat_weight: float,
    priority_weight: float,
) -> SchedulePlan:
    objective = _score_values(
        travel_minutes=total_travel_minutes,
        raw_exposure_units=candidate.raw_exposure_units,
        adjusted_exposure_units=candidate.adjusted_exposure_units,
        strategy=strategy,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
        priority_weighted_delay_minutes=(candidate.priority_weighted_delay_minutes),
    )
    stops = tuple(
        ScheduledStop(
            sequence=index + 1,
            job_id=draft.job_id,
            job_name=draft.job_name,
            latitude=draft.latitude,
            longitude=draft.longitude,
            arrival_minute=draft.arrival_minute,
            start_minute=draft.start_minute,
            finish_minute=draft.finish_minute,
            inbound_travel_minutes=draft.inbound_travel_minutes,
            temperature_c=round(draft.conditions.mean_temperature_c, 2),
            peak_temperature_c=round(draft.conditions.peak_temperature_c, 2),
            certainty=round(draft.conditions.mean_certainty, 4),
            raw_exposure_units=round(draft.conditions.degree_hours_above_reference, 3),
            certainty_adjusted_units=round(
                draft.conditions.certainty_adjusted_degree_hours, 3
            ),
            minutes_above_planning_threshold=round(
                draft.conditions.minutes_at_or_above_threshold, 1
            ),
        )
        for index, draft in enumerate(candidate.stops)
    )
    return SchedulePlan(
        strategy=strategy,
        stops=stops,
        total_travel_minutes=total_travel_minutes,
        total_raw_exposure_units=round(candidate.raw_exposure_units, 3),
        total_adjusted_exposure_units=round(candidate.adjusted_exposure_units, 3),
        minutes_above_planning_threshold=round(candidate.threshold_minutes, 1),
        priority_weighted_delay_minutes=round(
            candidate.priority_weighted_delay_minutes, 1
        ),
        route_finish_minute=route_finish_minute,
        objective_value=round(objective, 3),
    )


def _candidate_score(
    candidate: _Candidate,
    strategy: ScheduleStrategy,
    *,
    heat_weight: float,
    priority_weight: float,
) -> float:
    return _score_values(
        travel_minutes=candidate.travel_minutes,
        raw_exposure_units=candidate.raw_exposure_units,
        adjusted_exposure_units=candidate.adjusted_exposure_units,
        strategy=strategy,
        heat_weight=heat_weight,
        priority_weight=priority_weight,
        priority_weighted_delay_minutes=(candidate.priority_weighted_delay_minutes),
    )


def _score_values(
    *,
    travel_minutes: int,
    raw_exposure_units: float,
    adjusted_exposure_units: float,
    strategy: ScheduleStrategy,
    heat_weight: float,
    priority_weight: float,
    priority_weighted_delay_minutes: float,
) -> float:
    priority_cost = priority_weight * priority_weighted_delay_minutes
    if strategy in (ScheduleStrategy.ORIGINAL, ScheduleStrategy.EFFICIENCY):
        return travel_minutes + priority_cost
    if strategy is ScheduleStrategy.HEAT_AWARE:
        return travel_minutes + heat_weight * raw_exposure_units + priority_cost
    return travel_minutes + heat_weight * adjusted_exposure_units + priority_cost


def _validate_inputs(
    jobs: list[Job],
    profiles: dict[str, TemperatureProfile],
    *,
    average_travel_speed_kph: float,
    heat_weight: float,
    priority_weight: float,
    uncertainty_penalty: float,
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
    if priority_weight < 0:
        raise ValueError("priority_weight cannot be negative")
    if uncertainty_penalty < 0:
        raise ValueError("uncertainty_penalty cannot be negative")


def _travel_minutes(
    start: GeoPoint,
    end: GeoPoint,
    *,
    average_travel_speed_kph: float,
) -> int:
    return _cached_travel_minutes(
        start.latitude,
        start.longitude,
        end.latitude,
        end.longitude,
        average_travel_speed_kph,
    )


@lru_cache(maxsize=16384)
def _cached_travel_minutes(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
    average_travel_speed_kph: float,
) -> int:
    """Cache by plain coordinates: beam search revisits few distinct pairs."""

    if (start_latitude, start_longitude) == (end_latitude, end_longitude):
        return 0
    distance_km = _haversine_km(
        start_latitude, start_longitude, end_latitude, end_longitude
    )
    return max(1, ceil(distance_km / average_travel_speed_kph * 60))


def _haversine_km(
    start_latitude: float,
    start_longitude: float,
    end_latitude: float,
    end_longitude: float,
) -> float:
    earth_radius_km = 6371.0088
    lat1 = radians(start_latitude)
    lat2 = radians(end_latitude)
    delta_lat = lat2 - lat1
    delta_lon = radians(end_longitude - start_longitude)
    value = sin(delta_lat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(delta_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(value))


def _time_to_minute(value: time) -> int:
    return value.hour * 60 + value.minute
