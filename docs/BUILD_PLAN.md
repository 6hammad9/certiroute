# Build Plan

Status as of 2026-08-21: the vertical slice and deterministic four-strategy
comparison are working. The current focus is collecting the evidence needed to
replace synthetic certainty inputs with calibrated forecast intervals.

## Phase 1 — Data contract and vertical slice

- [x] Confirm the FortyGuard endpoints, payloads, units, timestamps, limits, and
  forecast behavior.
- [x] Define the job, temperature observation, risk estimate, and schedule models.
- [x] Build one API request through to one visible dashboard result.
- [x] Add a deterministic sample-data mode so the demo never depends entirely on a
  live external call.

## Phase 2 — Baseline scheduling

- [x] Implement the original-order and efficiency baselines.
- [x] Define and document the first heat-exposure score.
- [x] Integrate exposure and threshold duration over full job intervals.
- [x] Implement heat-aware and certainty-adjusted scheduling objectives.
- [x] Make priority operational through avoidable-delay cost.
- [x] Surface infeasibility without silently removing work from comparisons.
- [x] Add explicit time-window and infeasible-single-job tests.

## Phase 3 — Certainty-aware model

- [x] Add a normalized request cache and vendor-relative residual archive.
- [ ] Collect forecast/realization pairs across horizons and days.
- [ ] Choose stable, non-confounded reliability and distribution-shift features.
- [ ] Implement pooled and horizon-conditioned conformal intervals.
- [ ] Calibrate and evaluate intervals on held-out temporal/geographic data.
- [ ] Add coherent scenario objectives and probabilistic screening constraints.

## Phase 4 — Product demo

- [x] Build the first map, schedule table, comparison cards, and method panel.
- [x] Create one realistic customer scenario and one synthetic shifted scenario.
- [ ] Add a timeline, Pareto trade-off control, and downloadable results.
- [x] Add bounded polling, retry handling, caching primitives, and AOI guards.

## Phase 5 — Submission

- Deploy the application.
- Finalize methodology, limitations, and reproducibility instructions.
- Record the three-minute demo.
- Publish/review the repository and submit before the deadline buffer.
