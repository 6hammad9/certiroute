# FortyGuard API Integration Notes

Last checked against the official documentation and one live request on
2026-08-22.

## Implemented contract

- Versioned base URL: `https://api.fortyguard.com/v1`
- Authentication: `api-key` request header; no Bearer token
- Submit: `POST heatmap` relative to the versioned base URL
- Poll/result: `GET status/{activity_id}`
- First supported heatmap mode: `tcm`, single hour (`filter_type = 1`), numeric
  granularity `60`, `80`, or `100`
- AOI: U.S. GeoJSON `FeatureCollection` containing a closed `Polygon`; positions
  use `[longitude, latitude]`
- Polling: bounded, terminal on `Completed` or `Failed`, tolerant of an immediate
  transient `404`, and respectful of numeric `Retry-After` values

The application enforces the hackathon's stricter historical floor of
2021-01-01 even though the general API documentation currently advertises data
from 2019-01-01.

## Live verification

One deliberately small historical request was submitted for a roughly
200-by-200-metre area around central Phoenix at 100 m granularity. It completed
successfully and returned four polygon tiles.

Observed completed result structure:

```text
result
├── map_data
│   ├── type: FeatureCollection
│   └── features[]
│       ├── geometry.type: Polygon
│       └── properties
│           ├── tile_id
│           ├── average_temperature
│           ├── min_temperature
│           └── max_temperature
└── stats_data
    ├── temperature_stats
    ├── overall_temperature_distribution
    ├── normal_temperature_distribution
    └── temperature_frequency
```

The returned aggregate for this request was 39.76 °C minimum, mean, and maximum,
with 0.0 °C standard deviation. These values verify plumbing only; they are not
hard-coded product data or a model evaluation.

The first integrated scheduler path was verified with three heatmaps over a
compact five-job downtown portfolio. On 2026-08-21 it was replaced by the
current full-shift replay: ten completed hourly heatmaps (08:00–17:00) over six
Phoenix jobs in one 9.48 mi² AOI at 60 m granularity. All six coordinates were
covered at every hour. Example endpoint readings ranged from 37.1–37.7 °C at
08:00 and 40.1–40.2 °C at 17:00. The exact payloads and activity IDs remain in
the integrity-checked, Git-ignored local cache rather than source control.

## Scheduler data path

The schedule no longer uses the AOI aggregate as though every job had the same
temperature. Its real-data workflow now:

1. Validates 2–9 uploaded work orders and fingerprints the normalized manifest.
2. Deterministically partitions the job coordinates into one or more bounded
   AOIs, each no larger than the configured 10 mi² per-request boundary.
3. Proves that a complete depot-to-depot route can fit the entered shift and job
   windows before any network submission.
4. Retrieves the exact missing single-hour heatmaps for every AOI only after the
   operator clicks **Create my heat-aware route**.
5. Stores completed, secret-free raw results in an append-only local cache with
   request fingerprints and two independent SHA-256 integrity checks.
6. Parses every `map_data.features` Polygon/MultiPolygon, maps each job only to
   tiles from its assigned AOI, and merges the resulting per-job profiles.
7. Rejects missing, malformed, uncovered, or conflicting tile data rather than
   substituting an AOI average or synthetic value.
8. Builds per-job profiles from the requested hours and linearly interpolates
   between those real API samples during interval-exact schedule scoring.
9. Keeps request hours, activity IDs, collection timestamps, tile values,
   granularity, and saved/new provenance in **Planner details**, separate from
   the crew-facing route.

The optional Phoenix example uses ten hourly samples from 08:00 through 17:00.
Uploaded scenarios derive samples hourly from the confirmed shift and include
both boundaries. Historical exact requests can be reused indefinitely;
current/forecast cache support exists behind an explicit freshness TTL but is
not exposed in the first UI workflow yet. A request-fingerprint index avoids
reparsing unrelated large payloads when loading a prepared replay.

The API does not return a calibrated forecast-confidence probability. The
planner-facing result therefore compares only the operations baseline and
point-temperature heat-aware plan. The default crew view shows only the selected
stop order and hand-off controls. Neither view displays or implies a certainty
score; calibration remains future research.

## Important constraints

- Coverage is currently U.S.-only.
- Forecast heatmaps extend only through current time plus 12 hours.
- Basic/Startup heatmap AOIs are limited to 10 mi²; Premium is limited to
  50 mi².
- The client preflights every polygon against a configurable limit (10 mi² by
  default) before any POST. The live UI treats that limit as a per-request
  batching constraint, not as a selection radius: distributed portfolios are
  deterministically clustered, collected, cached, and merged across AOIs.
- Credits are deducted after successful task completion. Exact per-task credit
  calculation is not publicly specified, so requests should be spatially
  batched, cached, and kept no larger than necessary.
- A generic full-day range response is not documented as an hour-by-hour time
  series. CertiRoute will use single-hour requests until live evidence proves a
  stronger temporal contract.
- Normalized requests can be fingerprinted and archived locally without API
  credentials. Each forecast issuance and same-vendor realization is an
  immutable vintage. Since request timezone semantics remain undocumented,
  records preserve the request wall clock plus an explicit caller-supplied
  offset and label the derived UTC target/lead as assumptions. A realization
  request must match the selected forecast's hour, AOI, granularity, analytic
  type, and time assumption before residuals are calculated.

## Filter types and the absence of hourly forecasts

Verified by direct probing on 2026-08-22 with a 193-tile central Phoenix AOI
at 60 m. This is the single most consequential constraint on the product.

| Filter type | Behaviour | Current date |
| --- | --- | --- |
| 1 | One specific hour. Varies correctly by hour. | Returns **zero tiles** |
| 2 | Accepted, then completes with `n_cells: 0` | Empty |
| 3 | **Whole-day aggregate. `start_time` is ignored.** | Returns tiles |
| 4 | HTTP 500 | n/a |

Evidence that type 1 is historical-only: yesterday 14:00 returned 193 tiles,
while today at 08:00 (already past), 11:00, and 14:00 all returned a
*successful but empty* response - `{"map_data": {"features": []}}` with no
error. The boundary is the calendar day, not the 12-hour forecast horizon.

Evidence that type 3 ignores the hour: for the same past date, 06:00, 14:00
and 20:00 all returned a mean of exactly 37.9728 C, while filter type 1 gave
34.28 C at 06:00 and 41.82 C at 14:00.

### Hourly history lags by more than one day

Observed on 2026-08-23: every hour of 2026-08-22 returned zero tiles, while
2026-08-21 returned full data for all thirteen requested hours. The empty
window is therefore *at least* the current day and the one before it, not the
current day alone.

This matters more than it first appears, because the empty response is
**well-formed and reports success**. It arrives as `completed` with
`{"map_data": {"features": []}}` and no error field, so a naive collector
archives it as evidence. CertiRoute's snapshot store is append-only by design,
which means a cached empty answer is permanent: every later read is a cache hit
on nothing and the date can never be collected again. Thirteen such records
were written before this was caught. Empty results are now rejected in
`real_conditions._reject_empty_result` before they reach the store, and
`tests/test_real_conditions.py` holds the behaviour.

Practical consequence: a review or grading run should target dates at least two
days back, and any date can legitimately be uncollectable rather than merely
uncached.

### What this means

FortyGuard advertises 12-hour forecasting in its Temperature Dashboard, but
**no hourly forecast is reachable through this heatmap API**. Consequences:

- Hourly forecast-versus-realization residuals cannot be collected, so
  vendor forecast-skill calibration is blocked by the API surface, not by
  calendar time. Multi-lead prediction intervals are not currently possible.
- Same-day planning can only be anchored on the daily aggregate. This is what
  CertiRoute does: it learns the hourly *shape* offline from historical days
  and applies it to today's aggregate. See `src/certiroute/climatology.py`.
- Historical replay is fully supported and is the only mode with true hourly
  resolution, so it is what grading runs against.

Any claim about predicting future temperature must come from a model built on
historical data, never from the vendor, and must be qualified accordingly.

## Known documentation ambiguities

- Heatmap request timezone semantics are not documented. The UI must not label
  submitted times as local or UTC until this is confirmed.
- The Create Heatmap page mentions `filter_type = 4`, while Known Limitations and
  release notes list only types 1–3. The client currently supports type 1 only.
- FortyGuard describes temperature at 2 m above ground; this is measurement
  height, not map-cell resolution. Current heatmap granularity is 60/80/100 m.

## Official sources

- <https://docs-api.fortyguard.com/docs/authentication>
- <https://docs-api.fortyguard.com/docs/create-heatmap>
- <https://docs-api.fortyguard.com/docs/check-status>
- <https://docs-api.fortyguard.com/docs/quickstart>
- <https://docs-api.fortyguard.com/docs/limitations>
