# Build Plan

Status as of 2026-08-22: the product now presents one guided, real-data-only
historical-replay workflow for customer-supplied work orders. The primary path
is upload, validate, confirm the depot/date/shift, prove route feasibility, and
then collect only missing FortyGuard evidence. Its default **Crew route** stays
focused on one decision, one numbered map, an ordered stop list, and hand-off
controls. Comparison, exact temperatures, scoring, provenance, and detailed
safety limits remain in **Planner details**. The current research focus is the
evidence needed for calibrated forecast intervals.

## Phase 1 — Data contract and vertical slice

- [x] Confirm the historical FortyGuard endpoint, payloads, units, timestamps,
  and limits; document unresolved current/forecast time-zone semantics.
- [x] Define the job, temperature observation, risk estimate, and schedule models.
- [x] Build one API request through to one visible dashboard result.
- [x] Map returned temperature tiles to each job and feed them into scheduling.
- [x] Gate network collection behind one explicit build action and an append-only
  local API-response cache.
- [x] Add deterministic, API-shaped cached fixtures so automated UI tests never
  use the network or substitute product data.
- [x] Accept and normalize a UTF-8 customer manifest with eight required
  work-order columns, a 1 MB limit, and a 2–9-job product boundary; ignore
  unrelated export columns.
- [x] Fingerprint the normalized manifest and all route settings so input changes
  cannot reuse a stale in-memory result.

## Phase 2 — Baseline scheduling

- [x] Implement the original-order and efficiency baselines.
- [x] Define and document the first heat-exposure score.
- [x] Integrate exposure and threshold duration over full job intervals.
- [x] Implement heat-aware and certainty-adjusted scheduling objectives.
- [x] Make priority operational through avoidable-delay cost.
- [x] Surface infeasibility without silently removing work from comparisons.
- [x] Add explicit time-window and infeasible-single-job tests.
- [x] Prove that at least one full depot-to-depot route fits before any API
  submission.

## Phase 3 — Certainty-aware model

- [x] Add a normalized request cache and vendor-relative residual archive.
- [ ] Collect forecast/realization pairs across horizons and days.
- [ ] Choose stable, non-confounded reliability and distribution-shift features.
- [ ] Implement pooled and horizon-conditioned conformal intervals.
- [ ] Calibrate and evaluate intervals on held-out temporal/geographic data.
- [ ] Add coherent scenario objectives and probabilistic screening constraints.

## Phase 4 — Product demo

- [x] Rebuild the dashboard as one newcomer-friendly page with a compact
  three-step guide, one **Build heat-aware route** action, and separate
  crew/planner views.
- [x] Make a real customer CSV the primary input, with a downloadable template,
  clear validation errors, and job preview before planning.
- [x] Let the user confirm a depot, completed replay date, and same-day shift of
  at most 12 hours; reject service zones above 10 mi² and state FortyGuard's
  U.S.-only coverage boundary.
- [x] Remove the synthetic fallback, certainty controls, duplicate four-plan
  comparison, and API jargon from the customer path.
- [x] Make the default crew result a single numbered map with ordered stop cards,
  a clear first stop and depot return, plus Google Maps and CSV hand-off.
- [x] Move method comparison, exact modeled temperatures, source records,
  scoring assumptions, and detailed safety boundaries into **Planner details**.
- [x] Keep one optional judge-ready scenario spanning six Phoenix land-cover
  contexts, with fictional work orders explicitly labelled and real FortyGuard
  temperatures preserved.
- [x] Add bounded polling, retry handling, caching primitives, and AOI guards.
- [x] Replace the compact 0.78 mi², 230-work-minute API-plumbing portfolio with a
  9.48 mi², 410-work-minute Phoenix corridor. The real 2026-07-15 replay is a
  scientifically honest no-change result: the same route minimizes both the
  operational score and modeled heat load, with a maximum same-hour site spread
  of roughly 0.6 °C.
- [ ] Evaluate additional dates and customer portfolios; select a submission
  replay only from measured API outcomes, never by inventing temperature data.
- [ ] Add current-day and up-to-12-hour forecast routing only after the API's
  request-time-zone semantics are documented and verified.

## Phase 5 — Submission

- Deploy the application.
- Finalize methodology, limitations, and reproducibility instructions.
- Record the three-minute upload-to-crew-route demo, including validation,
  source evidence, and the optional Phoenix walkthrough.
- Publish/review the repository and submit before the deadline buffer.
