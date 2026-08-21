# Black-Box Forecast Reliability Method

## Claim boundary

CertiRoute's reliability layer is inspired by prospect certainty and
distribution-aware modeling in Yousef and Li (2025), but it does not reproduce
their algorithms. Those methods require model internals and reference
distributions that the FortyGuard API does not expose.

The current demo certainty values are scenario inputs used to exercise the
scheduler. They are not calibrated probabilities. Production UI must call them
reliability proxies until held-out coverage measurements support a stronger
claim.

Research lineage:

- [Prospect certainty for data-driven models](https://doi.org/10.1038/s41598-025-89679-6)
- [Chance Constrained Back-Mapping for Data-Driven Models](https://doi.org/10.36227/techrxiv.173893272.23694326/v1)

## What is implemented now

- Exact piecewise-linear exposure integration across every job interval.
- Exact fractional time above the configurable planning threshold.
- A pointwise certainty penalty integrated with exposure rather than applied
  only at a job midpoint.
- A secret-aware, append-only disk archive. A canonical request fingerprint
  groups related calls, while issuance time and activity ID preserve every
  distinct forecast vintage.
- Separate immutable realization records that can be joined to one explicitly
  selected forecast vintage without rewriting forecast history. The later API
  request and its time-basis assumption must match before residuals are formed.
- Explicit `realization - forecast` vendor-relative residuals.
- A deterministic AOI-clustering library primitive and a preflight area guard
  to prevent invalid or unexpectedly large API submissions. Automatic
  multi-request submission remains disabled until the UI can show the exact
  credit-consuming request count for explicit confirmation.

The residual is deliberately named **vendor-relative**. A later FortyGuard
result is useful for measuring forecast consistency, but it is not independent
ground truth. Cross-source observations may be added later as disagreement
features and must not be mislabeled as error without a validated observation
contract.

Tile joins use hashes of canonical Polygon/MultiPolygon geometry rather than
undocumented vendor tile IDs, and v1 residual records require exact spatial
coverage. Because the API's request-time timezone is undocumented, each
forecast record also stores the original wall clock and a required
caller-supplied fixed-offset assumption. Derived target and lead fields are
explicitly labeled `assumed_*`.

Cache envelopes include a canonical-payload SHA-256 checksum to detect
accidental on-disk corruption. This is an integrity check, not authentication;
a hostile writer with filesystem access could recompute it, so a MAC or digital
signature would be needed for tamper resistance.

## Calibration protocol

1. Archive forecasts as early as possible because residual data has a calendar
   dependency: the valid time must pass before a realization can be attached.
2. Archive every completed normalized request before adding any perturbation
   ensemble. If request memoization is added, keep it separate and require an
   explicit freshness window so an old forecast is never reused indefinitely.
3. Compare values at fixed job coordinates or stable tile identifiers. Do not
   treat a shifted AOI, changed granularity, or adjacent target hour as repeated
   measurements of the same quantity; those changes introduce real spatial,
   aggregation, or temporal variation.
4. Start with pooled residual quantiles, then split by forecast horizon once
   each group has enough samples.
5. Evaluate empirical interval coverage and average interval width on held-out
   days. Add Mondrian bins only after bin definitions are fixed in advance and
   each bin has enough exchangeable calibration examples.
6. Feed coherent whole-city or block-bootstrapped residual scenarios into the
   scheduler. Independent job-by-job temperature draws would create physically
   implausible city conditions.

`normal_temperature_distribution` in a FortyGuard response is documented as
normalized curve data for plotting the returned heatmap distribution. It is not
currently treated as a historical or training-distribution baseline. A distance
from the empirical spatial distribution may become a spatial-shape feature,
but calling it temporal out-of-distribution evidence would overstate the API
contract.

## Evidence required before probabilistic language

The submission should report, by forecast horizon and evaluation geography:

- empirical coverage at each nominal interval level;
- interval width;
- temperature residual MAE and bias;
- high-screening-temperature miss rate;
- schedule exposure and threshold minutes under later realizations;
- travel-time and priority-delay trade-offs.

Only measured coverage can justify phrases such as "90% prediction interval."
Until then, the dashboard should say "conservative reliability adjustment" and
show the inputs that caused it.

## Data and safety boundary

The archive rejects secret-like fields and stores geometry without arbitrary
GeoJSON properties. API keys remain in `.env` and are never part of cache keys
or payloads. Ambient temperature remains a planning screen, not a medical or
regulatory safety determination; see [SAFETY_MODEL.md](SAFETY_MODEL.md).
