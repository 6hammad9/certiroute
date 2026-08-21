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

## Important constraints

- Coverage is currently U.S.-only.
- Forecast heatmaps extend only through current time plus 12 hours.
- Basic/Startup heatmap AOIs are limited to 10 mi²; Premium is limited to
  50 mi².
- Credits are deducted after successful task completion. Exact per-task credit
  calculation is not publicly specified, so requests should be spatially
  batched, cached, and kept no larger than necessary.
- A generic full-day range response is not documented as an hour-by-hour time
  series. CertiRoute will use single-hour requests until live evidence proves a
  stronger temporal contract.

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

