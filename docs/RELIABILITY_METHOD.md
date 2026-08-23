# Black-Box Forecast Reliability Method

## Claim boundary

CertiRoute's reliability layer is inspired by prospect certainty and
distribution-aware modeling in Yousef and Li (2025), but it does not reproduce
their algorithms. Those methods require model internals and reference
distributions that the FortyGuard API does not expose.

The per-reading `certainty` field is a documented no-penalty sentinel of 1.0,
not a probability, and the product never presents it as one. Uncertainty
reaches the user through one number only: the calibrated interval radius.

That radius **is** now measured rather than asserted. Because CertiRoute builds
its own hourly prediction (the vendor exposes none), its residuals are
legitimately observable, which is what makes split-conformal calibration
possible at all. See "Calibration as it now stands" below.

Research lineage:

- [Prospect certainty for data-driven models](https://doi.org/10.1038/s41598-025-89679-6)
- [Chance Constrained Back-Mapping for Data-Driven Models](https://doi.org/10.36227/techrxiv.173893272.23694326/v1)

## Calibration as it now stands

The quantity calibrated is the error of *our* prediction of a day's hourly
curve, anchored on FortyGuard's whole-day aggregate for that day.

- **Conformity score:** one per held-out day - that day's largest absolute
  error across all sites and hours. Days are the exchangeable unit because a
  day runs hot or cool as a whole; pooling site-hours would claim far more
  independent evidence than exists and produce an interval that is too tight.
- **Coverage claimed:** only what the held-out day count can support, since a
  split-conformal radius needs `ceil((n+1)(1-alpha)) <= n`. Three held-out days
  support 75% and no more, and the app says 75%.
- **Direction of use:** schedules are built against the top of the interval,
  not its middle, so a hotter-than-predicted day still falls inside the
  assumption the start time was chosen under.

Measured on eleven contiguous Phoenix days at 60 m (train 09-16 Aug, calibrate
17-19 Aug):

| Quantity | Value |
| --- | --- |
| Held-out mean absolute error | 0.91 C |
| Worst held-out reading | 1.58 C |
| Error at sites left out of training | 0.91 C |
| Interval | +/- 1.58 C at 75% coverage |

The unseen-site figure matters more than it looks: the product applies one area
model to whatever points a dispatcher drops on the map, and this is the number
that says whether that is honest. It is identical to the day-holdout error,
which is the evidence that the diurnal shape is a regional property rather than
a per-site one.

### What is deliberately not calibrated

Day-ahead level prediction. Predicting tomorrow's level from past days measured
2.27 C mean absolute error with one day missed by 4.62 C. That is too loose to
put a crew's morning on, so the product does not offer it at any confidence.

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
