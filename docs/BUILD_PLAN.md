# Build Plan

Status as of 2026-08-21: the product now presents one guided, real-data-only
historical replay. It consumes ten hourly per-job FortyGuard heatmap tiles,
compares a distance-efficient operations baseline with a heat-aware schedule,
and keeps provenance and limitations below the decision. The current research
focus is collecting the evidence needed for calibrated forecast intervals.

## Phase 1 — Data contract and vertical slice

- [x] Confirm the FortyGuard endpoints, payloads, units, timestamps, limits, and
  forecast behavior.
- [x] Define the job, temperature observation, risk estimate, and schedule models.
- [x] Build one API request through to one visible dashboard result.
- [x] Map returned temperature tiles to each job and feed them into scheduling.
- [x] Gate network collection behind one explicit build action and an append-only
  local API-response cache.
- [x] Add deterministic, API-shaped cached fixtures so automated UI tests never
  use the network or substitute product data.

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

- [x] Rebuild the dashboard as one newcomer-friendly page with a three-step
  tutorial, one action, one recommendation, four business metrics, two-plan
  timeline, dispatcher sequence, and secondary evidence expanders.
- [x] Remove the synthetic fallback, certainty controls, duplicate four-plan
  comparison, and API jargon from the customer path.
- [x] Create one realistic demonstration scenario spanning six Phoenix land-cover
  contexts while keeping fictional work orders explicitly labelled.
- [ ] Add a timeline, Pareto trade-off control, and downloadable results.
- [x] Add bounded polling, retry handling, caching primitives, and AOI guards.
- [x] Replace the compact 0.78 mi², 230-work-minute API-plumbing portfolio with a
  9.48 mi², 410-work-minute Phoenix corridor. The real 2026-07-15 replay is a
  scientifically honest no-change result: the same route minimizes both the
  operational score and modeled heat load, with a maximum same-hour site spread
  of roughly 0.6 °C.
- [ ] Evaluate additional dates and customer portfolios; select a submission
  replay only from measured API outcomes, never by inventing temperature data.

## Phase 5 — Submission

- Deploy the application.
- Finalize methodology, limitations, and reproducibility instructions.
- Record the three-minute demo.
- Publish/review the repository and submit before the deadline buffer.
