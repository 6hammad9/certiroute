# Build Plan

Status as of 2026-08-22: the product now presents one guided, real-data-only
historical replay. Its default **Crew route** reduces the result to one decision,
one numbered map, an ordered stop list, and route hand-off controls. Scheduling
comparisons, exact modeled temperatures, scoring, provenance, and detailed
safety limits remain available in **Planner details**. The current research
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

- [x] Rebuild the dashboard as one newcomer-friendly page with a compact
  three-step guide, one **Build crew route** action, and separate crew/planner
  views.
- [x] Remove the synthetic fallback, certainty controls, duplicate four-plan
  comparison, and API jargon from the customer path.
- [x] Make the default crew result a single numbered map with ordered stop cards,
  a clear first stop and depot return, plus Google Maps and CSV hand-off.
- [x] Move method comparison, exact modeled temperatures, source records,
  scoring assumptions, and detailed safety boundaries into **Planner details**.
- [x] Create one realistic demonstration scenario spanning six Phoenix land-cover
  contexts while keeping fictional work orders explicitly labelled.
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
