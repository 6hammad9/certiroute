# FortyGuard API Integration Notes

Last checked against the official documentation and one live request on
2026-08-21.

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

1. Builds one shared, bounded AOI around the job coordinates.
2. Retrieves the exact missing single-hour heatmaps only after the operator
   clicks the page's single **Build heat-aware schedule** action.
3. Stores completed, secret-free raw results in an append-only local cache with
   request fingerprints and two independent SHA-256 integrity checks.
4. Parses every `map_data.features` Polygon/MultiPolygon and maps each job to the
   tile that geometrically covers its coordinate.
5. Rejects missing, malformed, uncovered, or conflicting tile data rather than
   substituting an AOI average or synthetic value.
6. Builds per-job profiles from the requested hours and linearly interpolates
   between those real API samples during interval-exact schedule scoring.
7. Displays request hours, activity IDs, collection timestamps, tile values,
   granularity, and saved/new provenance in a secondary evidence panel.

The default historical replay uses ten hourly samples from 08:00 through 17:00.
Historical exact requests can be reused indefinitely; current/forecast cache
support exists behind an explicit freshness TTL but is not exposed in the first
UI workflow yet. A request-fingerprint index avoids reparsing unrelated large
payloads when loading a prepared replay.

The API does not return a calibrated forecast-confidence probability. The
customer-facing result therefore compares only the operations baseline and
point-temperature heat-aware plan. It neither displays nor implies a certainty
score; calibration remains future research.

## Important constraints

- Coverage is currently U.S.-only.
- Forecast heatmaps extend only through current time plus 12 hours.
- Basic/Startup heatmap AOIs are limited to 10 mi²; Premium is limited to
  50 mi².
- The client preflights polygon area against a configurable limit (10 mi² by
  default) before any POST. A deterministic compact-clustering primitive is
  available for portfolios that need multiple AOIs. The live UI does not batch
  those requests automatically because every cluster is a separate credit-
  consuming task; explicit multi-request confirmation is planned.
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
