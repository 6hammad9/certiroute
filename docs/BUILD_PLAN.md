# Build Plan

Status as of 2026-08-22: the product now presents one guided, map-first,
real-data-only historical-replay workflow. The primary path is choose a U.S.
work area, click the crew base, click 2–9 work sites, and explicitly create the
route. Useful work-order and workday defaults keep coordinates, spreadsheets,
and advanced constraints out of first-time onboarding. CSV import and the
Phoenix walkthrough remain optional advanced paths. The default **Crew route**
stays focused on one decision, one numbered order preview, an ordered stop list,
and hand-off controls. Comparison, exact temperatures, scoring, provenance, and
detailed safety limits remain in **Planner details**. The current research focus
is the evidence needed for calibrated forecast intervals.

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
- [x] Model guided map state independently of Streamlit: city preset, first-click
  depot, 2–9 subsequent job sites, duplicate-event protection, undo/reset, and
  deterministic work-order creation.
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
  three-step guide, one **Create my heat-aware route** action, and separate
  crew/planner views.
- [x] Make direct map selection the primary input: choose a U.S. city preset,
  click once for the blue crew base, and click 2–9 orange work sites without
  entering latitude or longitude.
- [x] Create valid work orders from those clicks using a 45-minute duration,
  priority 3, and full-shift availability; expose only optional names and
  durations in the simple path.
- [x] Default to yesterday and an 08:00–17:00 workday while keeping a completed
  replay date and same-day shift of at most 12 hours available under an optional
  control; batch distributed jobs into independently validated AOIs of at most
  10 mi² and state FortyGuard's U.S.-only coverage boundary.
- [x] Keep CSV import as an advanced path for existing work-order exports, with
  a downloadable template, clear validation errors, and preview before placing
  the crew base.
- [x] Remove the synthetic fallback, certainty controls, duplicate four-plan
  comparison, and API jargon from the customer path.
- [x] Make the default crew result a single numbered map with ordered stop cards,
  a clear first stop and depot return, plus Google Maps and CSV hand-off.
- [x] Label the in-app connecting lines and travel times as straight-line
  estimates at 25 km/h; preserve the chosen order when handing stops to Google
  Maps for actual road directions instead of claiming road-route optimization.
- [x] Move method comparison, exact modeled temperatures, source records,
  scoring assumptions, and detailed safety boundaries into **Planner details**.
- [x] Keep one optional judge-ready scenario spanning six Phoenix land-cover
  contexts, with fictional work orders explicitly labelled and real FortyGuard
  temperatures preserved.
- [x] Add bounded polling, retry handling, caching primitives, and AOI guards.
- [x] Remove the misleading map-radius guide and transparently collect multiple
  bounded AOIs when selected jobs do not fit in one FortyGuard request.
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
- Record the three-minute map-click-to-crew-route demo, including validation,
  source evidence, road-navigation hand-off, and the optional advanced import
  and Phoenix walkthrough.
- Publish/review the repository and submit before the deadline buffer.
