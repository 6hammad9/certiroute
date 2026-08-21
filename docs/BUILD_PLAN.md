# Build Plan

## Phase 1 — Data contract and vertical slice

- Confirm the FortyGuard endpoints, payloads, units, timestamps, limits, and
  forecast behavior.
- Define the job, temperature observation, risk estimate, and schedule models.
- Build one API request through to one visible dashboard result.
- Add a deterministic sample-data mode so the demo never depends entirely on a
  live external call.

## Phase 2 — Baseline scheduling

- Implement the original-order and efficiency baselines.
- Define and document the first heat-exposure score.
- Implement a heat-aware scheduling objective.
- Add tests for time windows, durations, and infeasible schedules.

## Phase 3 — Certainty-aware model

- Choose reference/calibration data and distribution-shift features.
- Implement a simple, measurable certainty baseline.
- Calibrate the score on held-out temporal or geographic data.
- Add uncertainty-aware objectives and probabilistic constraints.

## Phase 4 — Product demo

- Build the map, schedule timeline, comparison cards, and explanation panel.
- Create one realistic customer scenario and one shifted-data scenario.
- Report safety/efficiency trade-offs and downloadable results.
- Add error handling, caching, and API-rate-limit protection.

## Phase 5 — Submission

- Deploy the application.
- Finalize methodology, limitations, and reproducibility instructions.
- Record the three-minute demo.
- Publish/review the repository and submit before the deadline buffer.

