# CertiRoute: Main Project Idea

## One-sentence pitch

CertiRoute uses FortyGuard's street-level temperature intelligence to build
certainty-aware daily schedules for mobile outdoor crews, minimizing cumulative
heat exposure while respecting travel time, job duration, priority, and
deadlines.

## Hackathon positioning

- **Primary track:** Track 3 — Industrial & Enterprise
- **Technical overlap:** Track 5 — Model Designing
- **Initial customers:** utilities, telecom operators, construction firms,
  municipal public-works teams, field-maintenance companies, and last-mile
  operators
- **Initial U.S. demonstration geography:** one heat-exposed metropolitan area,
  selected after validating API coverage and demo data quality

The commercial product is an operational planning tool. The certainty model is
its technical differentiator, rather than the product being presented only as a
forecasting experiment.

## The problem

Mobile crews may visit several outdoor job sites in one shift. Conventional
schedulers optimize distance, time, and deadlines while implicitly treating
environmental exposure as uniform across a city. A generic heat dashboard or
threshold alert identifies danger, but often arrives too late to improve the
day's plan.

Temperature predictions also have unequal reliability. Unusual weather,
seasonal change, unfamiliar locations, missing context, or sensor/data drift can
move deployment inputs away from the model's training distribution. A system
that hides this uncertainty can give an operator false confidence.

## The proposed solution

Given a list of jobs, CertiRoute will:

1. Retrieve historical, current-day, and available forecast temperature data
   from FortyGuard for each job location and time window.
2. Estimate heat exposure over each job and over the worker's full shift.
3. estimate how trustworthy each risk prediction is under the current input
   conditions.
4. Optimize job order and timing subject to travel, duration, priority,
   deadline, and uncertainty-aware heat-screening constraints.
5. Present the recommended schedule, the important trade-offs, and the expected
   exposure avoided relative to the original or shortest-time schedule.

Example output:

> Move Site C from 13:10 to 08:40. The optimized schedule is expected to reduce
> cumulative heat exposure by 28% while adding 11 minutes of travel. Prediction
> certainty is high. Site D remains uncertain and is flagged for supervisor
> review.

## What makes it different

Most heat applications follow this pattern:

> Read temperature → display a map → issue a threshold alert.

CertiRoute follows a different pattern:

> Forecast exposure → assess whether the estimate is trustworthy → optimize an
> operational decision under uncertainty → quantify the avoided risk.

The project combines five elements:

- Hyperlocal, time-dependent temperature intelligence
- Cumulative exposure rather than only instantaneous thresholds
- Explicit prediction certainty and distribution-shift awareness
- Constrained route and schedule optimization
- A measurable modeled-exposure-versus-efficiency business outcome

The core claim is not merely "routing around heat." It is **planning
conservatively when the heat-risk estimates themselves may be uncertain**.

## Connection to the professor's research

The reliability layer is inspired by two papers by Qais Yousef and Pu Li:

1. [Prospect certainty for data-driven models](https://doi.org/10.1038/s41598-025-89679-6)
   motivates an explicit measure of output certainty when deployment input
   distributions change.
2. [Chance Constrained Back-Mapping for Data-Driven Models](https://doi.org/10.36227/techrxiv.173893272.23694326/v1)
   addresses distribution shift through constrained back-mapping and
   probabilistic guarantees.

The hackathon prototype will adapt these principles rather than claim to
reproduce either paper completely. Its initial reliability pipeline will:

1. Detect inputs that differ from the calibration/reference distribution.
2. Produce a calibrated certainty or uncertainty score for each risk estimate.
3. Increase conservative planning margins or request review when certainty is
   low.
4. Incorporate uncertainty into chance-constrained scheduling.

A conceptual screening constraint is:

```text
P(worker exposure remains below the selected risk limit) >= target confidence
```

The target confidence must be configurable and described as a planning aid, not
as a guarantee of medical safety.

## MVP scope

The first complete demo should support:

- Upload or entry of 5–15 jobs with coordinates, duration, priority, and time
  windows
- FortyGuard temperature lookup for each relevant place and time
- A documented, configurable heat-exposure score
- Three comparable schedules:
  1. user-provided or original order
  2. shortest-time/distance baseline
  3. certainty-aware heat-optimized schedule
- A map and timeline for the recommended schedule
- Per-job risk and certainty indicators
- Summary metrics: cumulative exposure, high-risk minutes, travel time, jobs
  completed, and exposure reduction
- A deliberately shifted or uncertain demo scenario that shows conservative
  replanning

## Non-goals for the MVP

- Diagnosing heat illness or replacing occupational-health guidance
- Claiming certainty guarantees before they are empirically calibrated
- Building a general-purpose navigation engine from scratch
- Training another generic ambient-temperature model when FortyGuard already
  provides temperature intelligence
- Supporting every occupation, city, and heat-risk policy in the first version

## Research question

> Can explicit prediction certainty and chance-constrained optimization produce
> outdoor-work schedules with lower modeled screening risk than schedules
> optimized using point temperature estimates alone?

## Evaluation design

Compare three systems on identical job sets:

1. **Efficiency baseline:** optimize travel/time only.
2. **Heat-aware baseline:** optimize using point heat estimates.
3. **CertiRoute:** optimize certainty-adjusted exposure with probabilistic
   constraints.

Measure:

- Cumulative heat-exposure score
- Minutes above the selected risk threshold
- Violations or missed dangerous periods
- Added travel time and jobs completed
- Exposure reduction relative to both baselines
- Certainty calibration (for example, Brier score or calibration error once the
  target and labels are defined)
- Performance under temporal, geographic, and synthetic distribution shifts

## Three-minute demo story

1. A dispatcher uploads today's field jobs.
2. The conventional shortest route places exposed jobs in the hottest period.
3. CertiRoute rearranges the schedule and explains the exposure/time trade-off.
4. An unusual-condition scenario reduces forecast certainty.
5. The ordinary heat-aware plan stays overconfident; CertiRoute adds a
   conservative margin, replans, or flags the job for review.
6. The dashboard reports avoided exposure and operational cost in one sentence.

## Responsible-use boundary

CertiRoute is decision support, not medical advice. Heat risk depends on more
than ambient temperature, including humidity, solar radiation, workload,
clothing/PPE, acclimatization, health status, shade, and access to water and
rest. The UI and documentation must disclose which factors are and are not
modeled. Any occupational thresholds used in the prototype must be sourced and
configurable rather than silently hard-coded.

## Definition of hackathon success

The project succeeds when a judge can provide a small set of jobs and see a
working, explainable schedule that reduces modeled heat exposure, preserves
operational constraints, communicates uncertainty honestly, and demonstrates a
credible reason for a real organization to adopt it.
