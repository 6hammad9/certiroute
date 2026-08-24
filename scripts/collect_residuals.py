"""Collect FortyGuard forecast-versus-realization pairs over calendar time.

Forecast reliability cannot be calibrated from a single run. It needs pairs of
(what the vendor predicted for hour H) and (what the vendor later reported for
hour H), and those only accumulate as hours pass. This script captures both
halves and is safe to run repeatedly.

    python scripts/collect_residuals.py forecast --live   # capture predictions
    python scripts/collect_residuals.py realize  --live   # pair matured hours
    python scripts/collect_residuals.py status            # no network

FortyGuard's request-time timezone is undocumented, so every record stores an
explicit RequestTimeBasis rather than silently assuming one.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

from certiroute.collection import (
    ForecastArchive,
    ForecastRecord,
    RequestTimeBasis,
    TileForecast,
    VendorRelativeTileValue,
)
from certiroute.config import get_settings
from certiroute.domain import GeoPoint, Job
from certiroute.fortyguard import FortyGuardClient
from certiroute.fortyguard.geometry import bounding_polygon
from certiroute.fortyguard.schemas import HeatmapRequest, SingleHourDateTime

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_JOBS_PATH = PROJECT_ROOT / "data" / "sample" / "phoenix_jobs.csv"
ARCHIVE_PATH = PROJECT_ROOT / "data" / "raw" / "fortyguard_forecast_archive"

# Arizona does not observe daylight saving, so the offset is stable year round.
PHOENIX_OFFSET_MINUTES = -7 * 60
TIME_BASIS = RequestTimeBasis(
    assumption=(
        "Heatmap request wall clock is treated as Phoenix local time (UTC-7, "
        "no daylight saving). FortyGuard has not documented this semantic."
    ),
    utc_offset_minutes=PHOENIX_OFFSET_MINUTES,
)
GRANULARITY = 60
FORECAST_HORIZON_HOURS = 12


def load_jobs() -> list[Job]:
    """Load the committed demonstration work orders."""

    frame = pd.read_csv(SAMPLE_JOBS_PATH)
    return [
        Job(
            job_id=row.job_id,
            name=row.name,
            location=GeoPoint(latitude=row.latitude, longitude=row.longitude),
            duration_minutes=row.duration_minutes,
            priority=row.priority,
            earliest_start=time.fromisoformat(row.earliest_start),
            latest_finish=time.fromisoformat(row.latest_finish),
        )
        for row in frame.itertuples(index=False)
    ]


def build_request(target_local: datetime) -> HeatmapRequest:
    """One bounded AOI request for a single local hour."""

    return HeatmapRequest(
        polygon_aoi=bounding_polygon(job.location for job in load_jobs()),
        date_time=SingleHourDateTime(
            start_date=target_local.date(),
            start_time=time(target_local.hour, 0),
        ),
        granularity=GRANULARITY,
    )


def phoenix_now() -> datetime:
    """Current Phoenix wall clock, naive, matching the request semantics."""

    return (datetime.now(UTC) + timedelta(minutes=PHOENIX_OFFSET_MINUTES)).replace(
        tzinfo=None
    )


def tiles_from(result: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pull geometry, temperature, and vendor id from a completed heatmap."""

    map_data = result.get("map_data") or {}
    features = map_data.get("features") or []
    tiles: list[dict[str, Any]] = []
    for feature in features:
        properties = feature.get("properties") or {}
        temperature = properties.get("average_temperature")
        geometry = feature.get("geometry")
        if isinstance(temperature, bool) or not isinstance(temperature, (int, float)):
            continue
        if not isinstance(geometry, dict):
            continue
        tiles.append(
            {
                "geometry": geometry,
                "temperature": float(temperature),
                "vendor_tile_id": properties.get("tile_id"),
            }
        )
    return tiles


def open_client() -> FortyGuardClient:
    settings = get_settings()
    return FortyGuardClient(
        api_key=settings.fortyguard_api_key,
        base_url=settings.fortyguard_api_base_url,
        timeout_seconds=settings.fortyguard_timeout_seconds,
    )


def matured_targets(lookback_hours: int) -> list[datetime]:
    """Local hours that have already passed, most recent first."""

    now_local = phoenix_now().replace(minute=0, second=0, microsecond=0)
    return [
        now_local - timedelta(hours=offset) for offset in range(1, lookback_hours + 1)
    ]


def run_forecast(archive: ForecastArchive, hours: int, live: bool) -> None:
    """Capture predictions for upcoming hours inside the vendor horizon."""

    now_local = phoenix_now()
    horizon = min(hours, FORECAST_HORIZON_HOURS)
    targets = [
        (now_local + timedelta(hours=offset)).replace(minute=0, second=0, microsecond=0)
        for offset in range(1, horizon + 1)
    ]
    print(f"Phoenix local now : {now_local:%Y-%m-%d %H:%M}")
    print(f"Forecast targets  : {len(targets)} (horizon {horizon} h)")
    if not live:
        for target in targets:
            print(f"  would request {target:%Y-%m-%d %H:00}")
        print("\nPass --live to submit these requests.")
        return

    settings = get_settings()
    saved = 0
    with open_client() as client:
        for target in targets:
            request = build_request(target)
            requested_at = datetime.now(UTC)
            try:
                activity_id, result = client.create_heatmap(
                    request,
                    poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
                    max_attempts=settings.fortyguard_max_poll_attempts,
                )
            except Exception as exc:  # noqa: BLE001 - one bad hour must not stop the run
                print(f"  {target:%m-%d %H:00}  FAILED  {type(exc).__name__}: {exc}")
                continue
            tiles = tiles_from(result)
            if not tiles:
                print(f"  {target:%m-%d %H:00}  EMPTY   no tiles returned")
                continue
            record = archive.record_forecast(
                request,
                requested_at_utc=requested_at,
                request_time_basis=TIME_BASIS,
                activity_id=activity_id,
                per_tile_forecasts=[
                    TileForecast(
                        geometry=tile["geometry"],
                        forecast_temperature_c=tile["temperature"],
                        vendor_tile_id=tile["vendor_tile_id"],
                    )
                    for tile in tiles
                ],
            )
            mean = sum(tile["temperature"] for tile in tiles) / len(tiles)
            saved += 1
            print(
                f"  {target:%m-%d %H:00}  saved   {len(tiles):>5} tiles  "
                f"mean {mean:5.2f}C  lead {record.assumed_lead_hours:4.1f}h"
            )
    print(f"\nSaved {saved} forecast vintages.")


def run_realize(archive: ForecastArchive, lookback_hours: int, live: bool) -> None:
    """Fetch what actually materialised for hours previously forecast."""

    pending: list[tuple[datetime, ForecastRecord]] = []
    for target in matured_targets(lookback_hours):
        request = build_request(target)
        for forecast in archive.list_forecast_vintages(request):
            if archive.latest_vendor_relative_realization(forecast.record_id) is None:
                pending.append((target, forecast))

    print(f"Matured forecasts awaiting realization: {len(pending)}")
    if not pending:
        return
    if not live:
        for target, forecast in pending:
            print(
                f"  would realize {target:%Y-%m-%d %H:00}  ({forecast.record_id[:12]})"
            )
        print("\nPass --live to submit these requests.")
        return

    settings = get_settings()
    paired = 0
    with open_client() as client:
        for target, forecast in pending:
            request = build_request(target)
            try:
                activity_id, result = client.create_heatmap(
                    request,
                    poll_interval_seconds=settings.fortyguard_poll_interval_seconds,
                    max_attempts=settings.fortyguard_max_poll_attempts,
                )
            except Exception as exc:  # noqa: BLE001
                print(f"  {target:%m-%d %H:00}  FAILED  {type(exc).__name__}: {exc}")
                continue
            tiles = tiles_from(result)
            if not tiles:
                print(f"  {target:%m-%d %H:00}  EMPTY")
                continue
            try:
                record = archive.record_vendor_relative_realization(
                    forecast.record_id,
                    request=request,
                    request_time_basis=TIME_BASIS,
                    recorded_at_utc=datetime.now(UTC),
                    activity_id=activity_id,
                    per_tile_realizations=[
                        VendorRelativeTileValue(
                            geometry=tile["geometry"],
                            vendor_relative_realization_temperature_c=tile[
                                "temperature"
                            ],
                            vendor_tile_id=tile["vendor_tile_id"],
                        )
                        for tile in tiles
                    ],
                )
            except (ValueError, KeyError) as exc:
                print(f"  {target:%m-%d %H:00}  REJECTED  {exc}")
                continue
            paired += 1
            print(
                f"  {target:%m-%d %H:00}  paired  residual "
                f"{record.mean_vendor_relative_residual_c:+.2f}C  "
                f"lead {forecast.assumed_lead_hours:4.1f}h"
            )
    print(f"\nPaired {paired} forecasts with their realization.")


def run_status(archive: ForecastArchive, lookback_hours: int) -> None:
    """Report pair counts and residual spread without touching the network."""

    forecasts = 0
    residuals: list[tuple[float, float]] = []
    for target in matured_targets(lookback_hours):
        request = build_request(target)
        for forecast in archive.list_forecast_vintages(request):
            forecasts += 1
            realization = archive.latest_vendor_relative_realization(forecast.record_id)
            if realization is not None:
                residuals.append(
                    (
                        forecast.assumed_lead_hours,
                        realization.mean_vendor_relative_residual_c,
                    )
                )

    print(f"Archive         : {ARCHIVE_PATH}")
    print(f"Window          : last {lookback_hours} h")
    print(f"Matured forecasts: {forecasts}")
    print(f"Pairs complete  : {len(residuals)}")
    if not residuals:
        print("\nNo residual pairs yet. Run 'forecast' now, 'realize' later.")
        return
    values = [value for _, value in residuals]
    mean = sum(values) / len(values)
    print(f"Mean residual   : {mean:+.2f} C over {len(values)} pairs")
    print(f"Residual range  : {min(values):+.2f} to {max(values):+.2f} C")
    print("\n  lead(h)  residual(C)")
    for lead, value in sorted(residuals):
        print(f"  {lead:6.1f}  {value:+10.2f}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("forecast", "realize", "status"))
    parser.add_argument("--live", action="store_true", help="Actually call the API.")
    parser.add_argument("--hours", type=int, default=FORECAST_HORIZON_HOURS)
    parser.add_argument("--lookback-hours", type=int, default=72)
    args = parser.parse_args()

    archive = ForecastArchive(ARCHIVE_PATH)
    if args.mode == "forecast":
        run_forecast(archive, args.hours, args.live)
    elif args.mode == "realize":
        run_realize(archive, args.lookback_hours, args.live)
    else:
        run_status(archive, args.lookback_hours)


if __name__ == "__main__":
    main()
