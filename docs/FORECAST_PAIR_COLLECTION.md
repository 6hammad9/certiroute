# Forecast / Vendor-Relative Realization Collection

This workflow accumulates the evidence needed to calibrate FortyGuard forecast
intervals. It never calls a later FortyGuard value "ground truth": both sides
come from the same vendor, so the measured quantity is explicitly
`vendor_relative_realization_minus_forecast`.

## Current safety state

The official Heatmap documentation permits requests up to 12 hours ahead, but
it does not state which timezone is used for `start_date` and `start_time`. The
returned heatmap schema also does not echo an authoritative forecast valid-time.
Consequently, CertiRoute can safely plan requests, archive already-cached
pre-target responses, attach exact cached post-target responses, and report
status—but it **blocks every new forecast or realization POST**.

There is deliberately no command-line override. Enabling live submission
requires a written vendor contract for request-time interpretation and an
integration test proving that the requested and returned valid times agree.

Contract pages checked on 2026-08-22:

- <https://docs-api.fortyguard.com/docs/create-heatmap>
- <https://docs-api.fortyguard.com/docs/limitations>
- <https://docs-api.fortyguard.com/docs/check-status>

## Request manifest

Forecast planning accepts an exact, secret-free JSON manifest. The fixed UTC
offset is explicitly an assumption, not a statement about vendor behavior.

```json
{
  "manifest_schema_version": 1,
  "request_time_basis": {
    "source": "caller_supplied_assumption",
    "assumption": "Example only: wall clock treated as UTC-7; not vendor-confirmed.",
    "utc_offset_minutes": -420
  },
  "requests": [
    {
      "polygon_aoi": {
        "type": "FeatureCollection",
        "features": [
          {
            "type": "Feature",
            "properties": {},
            "geometry": {
              "type": "Polygon",
              "coordinates": [[
                [-112.01, 33.44],
                [-112.00, 33.44],
                [-112.00, 33.45],
                [-112.01, 33.44]
              ]]
            }
          }
        ]
      },
      "date_time": {
        "start_date": "2026-08-22",
        "start_time": "15:00",
        "filter_type": 1
      },
      "granularity": 100,
      "analytic_type": "tcm"
    }
  ]
}
```

Every request must be strictly in the assumed future and no more than 12 hours
ahead. Duplicate exact requests are rejected.

## Scheduled commands

All commands avoid API submissions and forecast/realization record writes unless
`--live` is supplied. A plan may synchronize the snapshot cache's integrity
index while checking exact cache hits:

```powershell
# Exact task/cache plan; no evidence-record writes and no network
.\.venv\Scripts\python.exe scripts\manage_forecast_pairs.py forecast requests.json

# List matured pairs that need a later-vendor value; no writes and no network
.\.venv\Scripts\python.exe scripts\manage_forecast_pairs.py realize

# Stable JSON for a scheduler/monitor
.\.venv\Scripts\python.exe scripts\manage_forecast_pairs.py status --json

# Audit rows for analysis
.\.venv\Scripts\python.exe scripts\manage_forecast_pairs.py report --format csv `
  --output data\processed\vendor_relative_pairs.csv
```

A mutating run always requires a hard task cap:

```powershell
.\.venv\Scripts\python.exe scripts\manage_forecast_pairs.py realize `
  --live --max-new-tasks 0
```

With a cap of zero, exact cached realizations may be attached without spending
credits. A positive cap still cannot bypass the unverified-time-contract block.
Every status and audit row remains marked `unverified_caller_assumption`; these
records are not calibration-ready until the vendor time contract is confirmed.

## Temporal and identity guarantees

- A forecast result must exist before its assumed target; a result completing
  at or after the target is rejected as lookahead-contaminated.
- A single-hour realization is not eligible until the full one-hour window has
  ended. `--settling-delay-minutes` can extend, but never shorten, that 60-minute
  minimum.
- The realization planner does not even query the snapshot cache for an
  immature forecast.
- AOI geometry, target hour, granularity, analytic type, and caller time-basis
  assumption must match the selected forecast.
- Polygon tiles are joined by canonical geometry, not undocumented vendor IDs.
- Multiple forecast vintages for one exact request share one later-vendor API
  task, keeping the credit cap tied to actual submissions rather than records.
- Forecasts, snapshots, and vendor-relative realizations remain append-only.

The implementation is in
`src/certiroute/collection/pair_workflow.py`; the operational entry point is
`scripts/manage_forecast_pairs.py`.
