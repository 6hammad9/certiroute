# Build Plan

Status as of 2026-08-21: the scheduler now consumes per-job FortyGuard heatmap
tiles in a credit-gated historical replay, while retaining an explicitly
labelled offline fallback. The current research focus is collecting the evidence
needed to replace synthetic certainty inputs with calibrated forecast intervals.

## Phase 1 — Data contract and vertical slice

- [x] Confirm the FortyGuard endpoints, payloads, units, timestamps, limits, and
  forecast behavior.
- [x] Define the job, temperature observation, risk estimate, and schedule models.
- [x] Build one API request through to one visible dashboard result.
- [x] Map returned temperature tiles to each job and feed them into scheduling.
- [x] Gate multi-hour collection behind an exact request count and local cache.
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

- [x] Build the first map, timeline, schedule table, comparison cards, and method
  panel.
- [x] Create one realistic customer scenario and one synthetic shifted scenario.
- [ ] Add a timeline, Pareto trade-off control, and downloadable results.
- [x] Add bounded polling, retry handling, caching primitives, and AOI guards.
- [ ] Replace the compact API-plumbing portfolio with a full-shift Phoenix
  scenario spanning meaningfully different microclimates, while keeping every
  requested AOI and task count explicit. The verified downtown sample has only
  about 0.01–0.04 °C tile standard deviation, so it proves integration but is
  not yet the strongest optimization story.

## Phase 5 — Submission

- Deploy the application.
- Finalize methodology, limitations, and reproducibility instructions.
- Record the three-minute demo.
- Publish/review the repository and submit before the deadline buffer.
