"""Leakage-safe rolling-origin evaluation on historical temperature profiles.

The point forecast in this module is deliberately modest: last observation
carried forward from an exact issuance-time measurement.  Its purpose is to
provide a reproducible retrospective baseline while real forecast vintages are
still being collected.  These results are not validation of a live FortyGuard
forecast and every output model carries that provenance explicitly.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import date, time
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from certiroute.domain import GeoPoint, Job
from certiroute.optimization.models import (
    ConditionPoint,
    SchedulePlan,
    ScheduleStrategy,
    TemperatureProfile,
)
from certiroute.optimization.scenarios import (
    ChanceConstraint,
    CityResidualVector,
    ResidualPoint,
    ScenarioRouteScore,
    bootstrap_city_residual_scenarios,
    compare_realized_decision,
    optimize_scenario_aware_order,
)
from certiroute.optimization.scheduler import optimize_job_order
from certiroute.reliability import (
    FiniteSampleQuantile,
    finite_sample_absolute_residual_quantile,
)

EVIDENCE_KIND = "retrospective_persistence_baseline"
FORECAST_METHOD = "exact_issuance_last_observation_carried_forward"
INTERPRETATION = "not_live_fortyguard_forecast_validation"


class LeakageSafePersistenceError(ValueError):
    """A persistence pair cannot be built without using future information."""


class InsufficientRollingHistoryError(ValueError):
    """A held-out origin has too few earlier, non-held-out residual days."""


class HistoricalRouteDay(BaseModel):
    """One dated route manifest and its later realized temperature profiles."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: str = Field(min_length=1, max_length=300)
    geography: str = Field(min_length=1, max_length=200)
    service_date: date
    source_label: str = Field(min_length=1, max_length=300)
    jobs: tuple[Job, ...] = Field(min_length=2, max_length=9)
    depot: GeoPoint
    realized_profiles: tuple[TemperatureProfile, ...] = Field(min_length=2)

    @field_validator("case_id", "geography", "source_label")
    @classmethod
    def normalize_label(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("identifier and source fields cannot be blank")
        return normalized

    @model_validator(mode="after")
    def validate_manifest_coverage(self) -> HistoricalRouteDay:
        job_ids = [job.job_id for job in self.jobs]
        if len(job_ids) != len(set(job_ids)):
            raise ValueError("historical day job IDs must be unique")
        profile_ids = [profile.job_id for profile in self.realized_profiles]
        if len(profile_ids) != len(set(profile_ids)):
            raise ValueError("historical day profile job IDs must be unique")
        if set(profile_ids) != set(job_ids):
            raise ValueError("realized profiles must exactly cover the day's jobs")
        return self

    @property
    def realized_profiles_by_job(self) -> dict[str, TemperatureProfile]:
        return {profile.job_id: profile for profile in self.realized_profiles}


class BacktestHoldout(BaseModel):
    """Dates and geographies that are never admitted to calibration.

    Membership is the union: a case is held out when either its date or its
    case-folded geography is named.  This prevents a named geography from
    leaking through another date and a named date from leaking through another
    geography.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    dates: frozenset[date] = frozenset()
    geographies: frozenset[str] = frozenset()

    @field_validator("geographies", mode="before")
    @classmethod
    def normalize_geographies(cls, value: object) -> frozenset[str]:
        if value is None:
            return frozenset()
        normalized = frozenset(
            str(item).strip().casefold()
            for item in value  # type: ignore[union-attr]
        )
        if "" in normalized:
            raise ValueError("held-out geographies cannot contain blanks")
        return normalized

    @model_validator(mode="after")
    def require_explicit_holdout(self) -> BacktestHoldout:
        if not self.dates and not self.geographies:
            raise ValueError("at least one held-out date or geography is required")
        return self

    def contains(self, day: HistoricalRouteDay) -> bool:
        return (
            day.service_date in self.dates
            or day.geography.casefold() in self.geographies
        )


class HistoricalBacktestPartition(BaseModel):
    """Explicit calibration/evaluation membership before rolling cutoffs."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    calibration_days: tuple[HistoricalRouteDay, ...] = Field(min_length=1)
    evaluation_days: tuple[HistoricalRouteDay, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disjoint_case_ids(self) -> HistoricalBacktestPartition:
        calibration = {day.case_id for day in self.calibration_days}
        evaluation = {day.case_id for day in self.evaluation_days}
        if calibration & evaluation:
            raise ValueError("calibration and evaluation case IDs must be disjoint")
        return self


class PersistenceForecastPair(BaseModel):
    """A pre-realization persistence profile paired with later same-day values."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: str
    geography: str
    service_date: date
    source_label: str
    evidence_kind: Literal["retrospective_persistence_baseline"] = EVIDENCE_KIND
    forecast_method: Literal["exact_issuance_last_observation_carried_forward"] = (
        FORECAST_METHOD
    )
    validates_live_fortyguard_forecast: Literal[False] = False
    issuance_minute: int = Field(ge=0, lt=24 * 60)
    forecast_end_minute: int = Field(gt=0, lt=24 * 60)
    forecast_profiles: tuple[TemperatureProfile, ...] = Field(min_length=2)
    realized_profiles: tuple[TemperatureProfile, ...] = Field(min_length=2)

    @model_validator(mode="after")
    def validate_pair(self) -> PersistenceForecastPair:
        if self.forecast_end_minute <= self.issuance_minute:
            raise ValueError("forecast end must follow issuance")
        forecast_ids = [profile.job_id for profile in self.forecast_profiles]
        realized_ids = [profile.job_id for profile in self.realized_profiles]
        if len(forecast_ids) != len(set(forecast_ids)):
            raise ValueError("forecast profile job IDs must be unique")
        if set(forecast_ids) != set(realized_ids):
            raise ValueError("forecast and realized profiles must cover the same jobs")
        for profile in self.forecast_profiles:
            if profile.points[0].minute_of_day != self.issuance_minute:
                raise ValueError("persistence forecasts must begin at issuance")
            if profile.points[-1].minute_of_day != self.forecast_end_minute:
                raise ValueError(
                    "persistence forecasts must end at forecast_end_minute"
                )
            temperatures = [point.temperature_c for point in profile.points]
            if any(
                not math.isclose(value, temperatures[0], abs_tol=1e-12)
                for value in temperatures[1:]
            ):
                raise ValueError("persistence forecasts must be constant")
        return self

    @property
    def forecast_profiles_by_job(self) -> dict[str, TemperatureProfile]:
        return {profile.job_id: profile for profile in self.forecast_profiles}

    @property
    def realized_profiles_by_job(self) -> dict[str, TemperatureProfile]:
        return {profile.job_id: profile for profile in self.realized_profiles}


class DatedCityResidualVector(BaseModel):
    """One calibration day's coherent persistence-error trajectory."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    case_id: str
    geography: str
    service_date: date
    source_label: str
    evidence_kind: Literal["retrospective_persistence_baseline"] = EVIDENCE_KIND
    vector: CityResidualVector


class BacktestProvenance(BaseModel):
    """Machine-readable guard against overstating retrospective evidence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_kind: Literal["retrospective_persistence_baseline"] = EVIDENCE_KIND
    forecast_method: Literal["exact_issuance_last_observation_carried_forward"] = (
        FORECAST_METHOD
    )
    interpretation: Literal["not_live_fortyguard_forecast_validation"] = INTERPRETATION
    validates_live_fortyguard_forecast: Literal[False] = False
    source_labels: tuple[str, ...] = Field(min_length=1)


class RollingOriginCaseResult(BaseModel):
    """One route decision frozen at issuance and evaluated after realization."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_id: str
    geography: str
    service_date: date
    source_label: str
    evidence_kind: Literal["retrospective_persistence_baseline"] = EVIDENCE_KIND
    validates_live_fortyguard_forecast: Literal[False] = False
    issuance_minute: int = Field(ge=0, lt=24 * 60)
    case_seed: int
    calibration_case_ids: tuple[str, ...] = Field(min_length=1)
    calibration_vector_ids: tuple[str, ...] = Field(min_length=1)
    calibration_source_labels: tuple[str, ...] = Field(min_length=1)
    persistence_error_quantile: FiniteSampleQuantile | None = None
    ordinary_forecast_plan: SchedulePlan
    scenario_forecast_score: ScenarioRouteScore
    decision_changed: bool
    realized_exposure_delta_units: float
    realized_threshold_minutes_delta: float
    realized_travel_minutes_delta: int
    scenario_route_reduced_realized_exposure: bool

    @model_validator(mode="after")
    def validate_decision_flag(self) -> RollingOriginCaseResult:
        ordinary_order = tuple(
            stop.job_id for stop in self.ordinary_forecast_plan.stops
        )
        if self.decision_changed != (
            ordinary_order != self.scenario_forecast_score.order
        ):
            raise ValueError("decision_changed does not match the two frozen orders")
        return self


class BacktestAggregate(BaseModel):
    """Aggregate deltas use scenario route minus ordinary route.

    Negative exposure, threshold-minute, or travel deltas favor the coherent
    scenario route.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    case_count: int = Field(gt=0)
    decision_change_count: int = Field(ge=0)
    decision_change_rate: float = Field(ge=0, le=1)
    scenario_route_realized_exposure_better_count: int = Field(ge=0)
    total_realized_exposure_delta_units: float
    mean_realized_exposure_delta_units: float
    total_realized_threshold_minutes_delta: float
    mean_realized_threshold_minutes_delta: float
    total_realized_travel_minutes_delta: int
    mean_realized_travel_minutes_delta: float


class RollingOriginBacktestReport(BaseModel):
    """Complete retrospective report with explicit provenance and holdouts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provenance: BacktestProvenance
    holdout: BacktestHoldout
    cases: tuple[RollingOriginCaseResult, ...] = Field(min_length=1)
    aggregate: BacktestAggregate


def partition_historical_days(
    days: Sequence[HistoricalRouteDay],
    holdout: BacktestHoldout,
) -> HistoricalBacktestPartition:
    """Reserve the named dates/geographies before any residuals are fitted."""

    values = tuple(days)
    if not values:
        raise ValueError("at least one historical day is required")
    identifiers = [day.case_id for day in values]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("historical case IDs must be unique")
    ordered = tuple(
        sorted(values, key=lambda day: (day.service_date, day.geography, day.case_id))
    )
    calibration = tuple(day for day in ordered if not holdout.contains(day))
    evaluation = tuple(day for day in ordered if holdout.contains(day))
    if not calibration:
        raise ValueError("holdout leaves no calibration days")
    if not evaluation:
        raise ValueError("holdout selects no evaluation days")
    return HistoricalBacktestPartition(
        calibration_days=calibration,
        evaluation_days=evaluation,
    )


def build_persistence_forecast_pair(
    day: HistoricalRouteDay,
    *,
    issuance_minute: int,
    forecast_end_minute: int,
) -> PersistenceForecastPair:
    """Create a constant forecast using only the exact issuance observation.

    Future profile timestamps and values are not used to construct the point
    forecast.  The later values are copied into a separate realized profile and
    are accessed only during residual construction or final evaluation.
    """

    if not 0 <= issuance_minute < 24 * 60:
        raise ValueError("issuance_minute must be within the service day")
    if not issuance_minute < forecast_end_minute < 24 * 60:
        raise ValueError("forecast_end_minute must follow issuance within the day")

    forecast_profiles = []
    realized_profiles = []
    for job_id in sorted(day.realized_profiles_by_job):
        historical = day.realized_profiles_by_job[job_id]
        issuance_points = [
            point
            for point in historical.points
            if point.minute_of_day == issuance_minute
        ]
        if not issuance_points:
            raise LeakageSafePersistenceError(
                f"case {day.case_id!r}, job {job_id!r} has no exact "
                "issuance-time measurement; interpolation is refused"
            )
        if historical.points[-1].minute_of_day < forecast_end_minute:
            raise LeakageSafePersistenceError(
                f"case {day.case_id!r}, job {job_id!r} does not cover the forecast end"
            )
        issued_temperature = issuance_points[0].temperature_c
        forecast_profiles.append(
            TemperatureProfile(
                job_id=job_id,
                points=(
                    ConditionPoint(
                        minute_of_day=issuance_minute,
                        temperature_c=issued_temperature,
                        certainty=1,
                    ),
                    ConditionPoint(
                        minute_of_day=forecast_end_minute,
                        temperature_c=issued_temperature,
                        certainty=1,
                    ),
                ),
            )
        )

        realized_points = [issuance_points[0]]
        realized_points.extend(
            point
            for point in historical.points
            if issuance_minute < point.minute_of_day < forecast_end_minute
        )
        if historical.points[-1].minute_of_day == forecast_end_minute:
            realized_points.append(historical.points[-1])
        else:
            temperature, certainty = historical.condition_at(forecast_end_minute)
            realized_points.append(
                ConditionPoint(
                    minute_of_day=forecast_end_minute,
                    temperature_c=temperature,
                    certainty=certainty,
                )
            )
        realized_profiles.append(
            TemperatureProfile(job_id=job_id, points=tuple(realized_points))
        )

    return PersistenceForecastPair(
        case_id=day.case_id,
        geography=day.geography,
        service_date=day.service_date,
        source_label=day.source_label,
        issuance_minute=issuance_minute,
        forecast_end_minute=forecast_end_minute,
        forecast_profiles=tuple(forecast_profiles),
        realized_profiles=tuple(realized_profiles),
    )


def build_dated_city_residual_vector(
    pair: PersistenceForecastPair,
) -> DatedCityResidualVector:
    """Average site residuals at each knot while retaining the whole day block."""

    minutes = sorted(
        {
            point.minute_of_day
            for profile in pair.realized_profiles
            for point in profile.points
        }
    )
    forecast_profiles = pair.forecast_profiles_by_job
    realized_profiles = pair.realized_profiles_by_job
    points = []
    for minute in minutes:
        residuals = []
        for job_id in sorted(realized_profiles):
            forecast_temperature, _ = forecast_profiles[job_id].condition_at(minute)
            realized_temperature, _ = realized_profiles[job_id].condition_at(minute)
            residuals.append(realized_temperature - forecast_temperature)
        points.append(
            ResidualPoint(
                minute_of_day=minute,
                residual_c=sum(residuals) / len(residuals),
            )
        )
    return DatedCityResidualVector(
        case_id=pair.case_id,
        geography=pair.geography,
        service_date=pair.service_date,
        source_label=pair.source_label,
        vector=CityResidualVector(
            vector_id=f"retrospective-persistence:{pair.case_id}",
            points=tuple(points),
        ),
    )


def run_rolling_origin_backtest(
    days: Sequence[HistoricalRouteDay],
    *,
    holdout: BacktestHoldout,
    issuance_minute: int,
    shift_start: time = time(8, 0),
    shift_end: time = time(17, 0),
    average_travel_speed_kph: float = 25.0,
    reference_temperature_c: float = 27.0,
    planning_threshold_c: float = 35.0,
    heat_weight: float = 4.0,
    priority_weight: float = 0.2,
    scenario_count: int = 100,
    seed: int = 0,
    cvar_alpha: float = 0.9,
    cvar_weight: float = 0.5,
    chance_constraint: ChanceConstraint | None = None,
    minimum_calibration_days: int = 1,
    reliability_miscoverage: float | None = None,
    max_candidate_orders: int = 512,
    scenario_seed_limit: int = 12,
    beam_width: int = 1000,
) -> RollingOriginBacktestReport:
    """Run chronological, geography/date-held-out route backtests.

    For an evaluation case dated ``D``, only non-held-out cases dated before
    ``D`` provide residual vectors.  Both candidate routes are selected from
    the persistence forecast and those earlier vectors before the held-out
    realization is passed to the evaluation helper.
    """

    start_minute = _time_to_minute(shift_start)
    end_minute = _time_to_minute(shift_end)
    if shift_start.tzinfo is not None or shift_end.tzinfo is not None:
        raise ValueError("backtest shift times must be unzoned local wall times")
    if not issuance_minute <= start_minute < end_minute:
        raise ValueError(
            "issuance must not follow shift start, and shift must end later"
        )
    if scenario_count < 1:
        raise ValueError("scenario_count must be at least one")
    if minimum_calibration_days < 1:
        raise ValueError("minimum_calibration_days must be at least one")
    if reliability_miscoverage is not None and not 0 < reliability_miscoverage < 1:
        raise ValueError(
            "reliability_miscoverage must be strictly between zero and one"
        )

    partition = partition_historical_days(days, holdout)
    all_days = (*partition.calibration_days, *partition.evaluation_days)
    pairs = {
        day.case_id: build_persistence_forecast_pair(
            day,
            issuance_minute=issuance_minute,
            forecast_end_minute=end_minute,
        )
        for day in all_days
    }
    calibration_vectors = {
        day.case_id: build_dated_city_residual_vector(pairs[day.case_id])
        for day in partition.calibration_days
    }

    results = []
    for case_index, day in enumerate(partition.evaluation_days):
        eligible_days = tuple(
            calibration_day
            for calibration_day in partition.calibration_days
            if calibration_day.service_date < day.service_date
        )
        if len(eligible_days) < minimum_calibration_days:
            raise InsufficientRollingHistoryError(
                f"evaluation case {day.case_id!r} has {len(eligible_days)} earlier "
                f"non-held-out calibration days; {minimum_calibration_days} required"
            )
        eligible_vectors = tuple(
            calibration_vectors[calibration_day.case_id]
            for calibration_day in eligible_days
        )
        pair = pairs[day.case_id]
        point_profiles = pair.forecast_profiles_by_job
        case_seed = seed + case_index
        scenarios = bootstrap_city_residual_scenarios(
            point_profiles,
            [item.vector for item in eligible_vectors],
            scenario_count=scenario_count,
            seed=case_seed,
        )
        schedule_common = {
            "depot": day.depot,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "average_travel_speed_kph": average_travel_speed_kph,
            "reference_temperature_c": reference_temperature_c,
            "planning_threshold_c": planning_threshold_c,
            "heat_weight": heat_weight,
            "priority_weight": priority_weight,
        }
        ordinary_plan = optimize_job_order(
            list(day.jobs),
            point_profiles,
            strategy=ScheduleStrategy.HEAT_AWARE,
            uncertainty_penalty=0,
            beam_width=beam_width,
            **schedule_common,
        )
        ordinary_order = tuple(stop.job_id for stop in ordinary_plan.stops)
        scenario_result = optimize_scenario_aware_order(
            day.jobs,
            point_profiles,
            scenarios,
            chance_constraint=chance_constraint,
            cvar_alpha=cvar_alpha,
            cvar_weight=cvar_weight,
            max_candidate_orders=max_candidate_orders,
            scenario_seed_limit=scenario_seed_limit,
            beam_width=beam_width,
            **schedule_common,
        )
        scenario_order = scenario_result.recommended.order

        # Realized profiles enter only after both orders above are frozen.
        realized = compare_realized_decision(
            scenario_order,
            ordinary_order,
            day.jobs,
            pair.realized_profiles_by_job,
            **schedule_common,
        )
        reliability_quantile = _optional_reliability_quantile(
            eligible_vectors,
            issuance_minute=issuance_minute,
            miscoverage=reliability_miscoverage,
        )
        results.append(
            RollingOriginCaseResult(
                case_id=day.case_id,
                geography=day.geography,
                service_date=day.service_date,
                source_label=day.source_label,
                issuance_minute=issuance_minute,
                case_seed=case_seed,
                calibration_case_ids=tuple(item.case_id for item in eligible_vectors),
                calibration_vector_ids=tuple(
                    item.vector.vector_id for item in eligible_vectors
                ),
                calibration_source_labels=tuple(
                    sorted({item.source_label for item in eligible_vectors})
                ),
                persistence_error_quantile=reliability_quantile,
                ordinary_forecast_plan=ordinary_plan,
                scenario_forecast_score=scenario_result.recommended,
                decision_changed=ordinary_order != scenario_order,
                realized_exposure_delta_units=round(
                    -realized.exposure_units_avoided, 6
                ),
                realized_threshold_minutes_delta=round(
                    -realized.threshold_minutes_avoided, 6
                ),
                realized_travel_minutes_delta=realized.travel_minutes_difference,
                scenario_route_reduced_realized_exposure=(
                    realized.recommended_reduced_exposure
                ),
            )
        )

    cases = tuple(results)
    source_labels = tuple(sorted({day.source_label for day in all_days}))
    return RollingOriginBacktestReport(
        provenance=BacktestProvenance(source_labels=source_labels),
        holdout=holdout,
        cases=cases,
        aggregate=_aggregate_cases(cases),
    )


def _optional_reliability_quantile(
    vectors: Sequence[DatedCityResidualVector],
    *,
    issuance_minute: int,
    miscoverage: float | None,
) -> FiniteSampleQuantile | None:
    if miscoverage is None:
        return None
    absolute_residuals = (
        abs(point.residual_c)
        for item in vectors
        for point in item.vector.points
        if point.minute_of_day > issuance_minute
    )
    return finite_sample_absolute_residual_quantile(
        absolute_residuals,
        miscoverage=miscoverage,
    )


def _aggregate_cases(cases: Sequence[RollingOriginCaseResult]) -> BacktestAggregate:
    count = len(cases)
    changed = sum(case.decision_changed for case in cases)
    better = sum(case.scenario_route_reduced_realized_exposure for case in cases)
    exposure_total = sum(case.realized_exposure_delta_units for case in cases)
    threshold_total = sum(case.realized_threshold_minutes_delta for case in cases)
    travel_total = sum(case.realized_travel_minutes_delta for case in cases)
    return BacktestAggregate(
        case_count=count,
        decision_change_count=changed,
        decision_change_rate=changed / count,
        scenario_route_realized_exposure_better_count=better,
        total_realized_exposure_delta_units=round(exposure_total, 6),
        mean_realized_exposure_delta_units=round(exposure_total / count, 6),
        total_realized_threshold_minutes_delta=round(threshold_total, 6),
        mean_realized_threshold_minutes_delta=round(threshold_total / count, 6),
        total_realized_travel_minutes_delta=travel_total,
        mean_realized_travel_minutes_delta=round(travel_total / count, 6),
    )


def _time_to_minute(value: time) -> int:
    return value.hour * 60 + value.minute
