"""Leakage-safe retrospective evaluation utilities."""

from certiroute.evaluation.backtesting import (
    BacktestAggregate,
    BacktestHoldout,
    BacktestProvenance,
    DatedCityResidualVector,
    HistoricalBacktestPartition,
    HistoricalRouteDay,
    InsufficientRollingHistoryError,
    LeakageSafePersistenceError,
    PersistenceForecastPair,
    RollingOriginBacktestReport,
    RollingOriginCaseResult,
    build_dated_city_residual_vector,
    build_persistence_forecast_pair,
    partition_historical_days,
    run_rolling_origin_backtest,
)

__all__ = [
    "BacktestAggregate",
    "BacktestHoldout",
    "BacktestProvenance",
    "DatedCityResidualVector",
    "HistoricalBacktestPartition",
    "HistoricalRouteDay",
    "InsufficientRollingHistoryError",
    "LeakageSafePersistenceError",
    "PersistenceForecastPair",
    "RollingOriginBacktestReport",
    "RollingOriginCaseResult",
    "build_dated_city_residual_vector",
    "build_persistence_forecast_pair",
    "partition_historical_days",
    "run_rolling_origin_backtest",
]
