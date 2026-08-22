from datetime import date

import pytest

from certiroute.domain import GeoPoint, Job
from certiroute.evaluation import (
    BacktestHoldout,
    HistoricalRouteDay,
    InsufficientRollingHistoryError,
    LeakageSafePersistenceError,
    build_persistence_forecast_pair,
    partition_historical_days,
    run_rolling_origin_backtest,
)
from certiroute.optimization import ConditionPoint, TemperatureProfile


DEPOT = GeoPoint(latitude=33.45, longitude=-112.07)


def _jobs() -> tuple[Job, ...]:
    return (
        Job(
            job_id="A",
            name="Cool high-priority job",
            location=DEPOT,
            duration_minutes=30,
            priority=5,
        ),
        Job(
            job_id="B",
            name="Warmer low-priority job",
            location=DEPOT,
            duration_minutes=30,
            priority=1,
        ),
    )


def _realized_profile(
    job_id: str,
    base_temperature_c: float,
    residuals_c: tuple[float, float, float] = (10, -15, -15),
) -> TemperatureProfile:
    return TemperatureProfile(
        job_id=job_id,
        points=(
            ConditionPoint(
                minute_of_day=8 * 60,
                temperature_c=base_temperature_c,
                certainty=1,
            ),
            ConditionPoint(
                minute_of_day=9 * 60,
                temperature_c=base_temperature_c + residuals_c[0],
                certainty=1,
            ),
            ConditionPoint(
                minute_of_day=11 * 60,
                temperature_c=base_temperature_c + residuals_c[1],
                certainty=1,
            ),
            ConditionPoint(
                minute_of_day=17 * 60,
                temperature_c=base_temperature_c + residuals_c[2],
                certainty=1,
            ),
        ),
    )


def _day(
    case_id: str,
    service_date: date,
    geography: str,
    *,
    residuals_c: tuple[float, float, float] = (10, -15, -15),
    source_label: str = "FortyGuard historical heatmap replay",
) -> HistoricalRouteDay:
    return HistoricalRouteDay(
        case_id=case_id,
        geography=geography,
        service_date=service_date,
        source_label=source_label,
        jobs=_jobs(),
        depot=DEPOT,
        realized_profiles=(
            _realized_profile("A", 18, residuals_c),
            _realized_profile("B", 22, residuals_c),
        ),
    )


def test_persistence_forecast_does_not_change_when_future_values_change() -> None:
    first = _day("first", date(2026, 7, 1), "Tucson")
    second = _day(
        "second",
        date(2026, 7, 1),
        "Tucson",
        residuals_c=(-30, 50, 80),
    )

    first_pair = build_persistence_forecast_pair(
        first, issuance_minute=8 * 60, forecast_end_minute=17 * 60
    )
    second_pair = build_persistence_forecast_pair(
        second, issuance_minute=8 * 60, forecast_end_minute=17 * 60
    )

    assert first_pair.forecast_profiles == second_pair.forecast_profiles
    assert first_pair.realized_profiles != second_pair.realized_profiles
    for profile in first_pair.forecast_profiles:
        assert [point.minute_of_day for point in profile.points] == [8 * 60, 17 * 60]
        assert len({point.temperature_c for point in profile.points}) == 1
    assert first_pair.validates_live_fortyguard_forecast is False


def test_persistence_refuses_to_interpolate_an_issuance_value() -> None:
    jobs = _jobs()
    profiles = tuple(
        TemperatureProfile(
            job_id=job.job_id,
            points=(
                ConditionPoint(
                    minute_of_day=7 * 60 + 30,
                    temperature_c=20,
                    certainty=1,
                ),
                ConditionPoint(
                    minute_of_day=9 * 60,
                    temperature_c=30,
                    certainty=1,
                ),
                ConditionPoint(
                    minute_of_day=17 * 60,
                    temperature_c=25,
                    certainty=1,
                ),
            ),
        )
        for job in jobs
    )
    day = HistoricalRouteDay(
        case_id="missing-issuance",
        geography="Tucson",
        service_date=date(2026, 7, 1),
        source_label="historical sensors",
        jobs=jobs,
        depot=DEPOT,
        realized_profiles=profiles,
    )

    with pytest.raises(LeakageSafePersistenceError, match="interpolation is refused"):
        build_persistence_forecast_pair(
            day,
            issuance_minute=8 * 60,
            forecast_end_minute=17 * 60,
        )


def test_holdout_is_the_union_of_named_dates_and_geographies() -> None:
    days = (
        _day("old", date(2026, 7, 1), "Tucson"),
        _day("geo", date(2026, 7, 2), "Phoenix"),
        _day("dated", date(2026, 7, 3), "Tucson"),
        _day("later", date(2026, 7, 4), "Flagstaff"),
    )
    holdout = BacktestHoldout(
        dates=frozenset({date(2026, 7, 3)}),
        geographies=frozenset({"  PHOENIX  "}),
    )

    partition = partition_historical_days(days, holdout)

    assert {day.case_id for day in partition.calibration_days} == {"old", "later"}
    assert {day.case_id for day in partition.evaluation_days} == {"geo", "dated"}
    assert holdout.geographies == frozenset({"phoenix"})


def test_rolling_origin_changes_a_decision_and_confirms_it_after_realization() -> None:
    days = (
        _day("cal-1", date(2026, 7, 1), "Tucson"),
        _day("cal-2", date(2026, 7, 2), "Tucson"),
        _day("held", date(2026, 7, 3), "Phoenix"),
        # This is non-held-out but occurs after the evaluation origin and must
        # therefore never enter the held case's rolling calibration set.
        _day("future-cal", date(2026, 7, 4), "Tucson", residuals_c=(50, 50, 50)),
    )
    kwargs = {
        "holdout": BacktestHoldout(geographies=frozenset({"Phoenix"})),
        "issuance_minute": 8 * 60,
        "reference_temperature_c": 27,
        "planning_threshold_c": 27,
        "heat_weight": 10,
        "priority_weight": 0.2,
        "scenario_count": 8,
        "seed": 91,
        "cvar_alpha": 0.5,
        "cvar_weight": 1,
        "minimum_calibration_days": 2,
        "reliability_miscoverage": 0.5,
    }

    report = run_rolling_origin_backtest(days, **kwargs)
    repeated = run_rolling_origin_backtest(days, **kwargs)

    assert report == repeated
    assert len(report.cases) == 1
    result = report.cases[0]
    ordinary_order = tuple(stop.job_id for stop in result.ordinary_forecast_plan.stops)
    assert ordinary_order == ("A", "B")
    assert result.scenario_forecast_score.order == ("B", "A")
    assert result.decision_changed is True
    assert result.calibration_case_ids == ("cal-1", "cal-2")
    assert "future-cal" not in result.calibration_case_ids
    assert result.realized_exposure_delta_units < 0
    assert result.realized_threshold_minutes_delta < 0
    assert result.scenario_route_reduced_realized_exposure is True
    assert result.persistence_error_quantile is not None
    assert result.persistence_error_quantile.sample_count == 6

    aggregate = report.aggregate
    assert aggregate.case_count == 1
    assert aggregate.decision_change_count == 1
    assert aggregate.decision_change_rate == 1
    assert aggregate.mean_realized_exposure_delta_units == pytest.approx(
        result.realized_exposure_delta_units
    )
    assert aggregate.mean_realized_threshold_minutes_delta == pytest.approx(
        result.realized_threshold_minutes_delta
    )
    assert aggregate.mean_realized_travel_minutes_delta == 0

    assert report.provenance.evidence_kind == "retrospective_persistence_baseline"
    assert report.provenance.interpretation == (
        "not_live_fortyguard_forecast_validation"
    )
    assert report.provenance.validates_live_fortyguard_forecast is False
    assert report.provenance.source_labels == ("FortyGuard historical heatmap replay",)


def test_rolling_origin_requires_earlier_non_holdout_history() -> None:
    days = (
        _day("held", date(2026, 7, 1), "Phoenix"),
        _day("later", date(2026, 7, 2), "Tucson"),
    )

    with pytest.raises(InsufficientRollingHistoryError, match="0 earlier"):
        run_rolling_origin_backtest(
            days,
            holdout=BacktestHoldout(geographies=frozenset({"Phoenix"})),
            issuance_minute=8 * 60,
            scenario_count=2,
        )
