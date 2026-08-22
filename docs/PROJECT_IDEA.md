# CertiRoute: Main Project Idea

## One-sentence pitch

CertiRoute uses FortyGuard's street-level temperature intelligence to decide
whether a mobile crew's work order should change, reducing modeled cumulative
heat exposure when the benefit justifies the travel cost while preserving job
duration, priority, deadlines, and depot return.

## Current product slice

The working interface is deliberately narrower than the full research vision:

- six fictional work orders at real Phoenix landmarks;
- ten hourly FortyGuard heatmaps with no substitute temperature data;
- one distance-efficient operations baseline and one heat-aware recommendation;
- an explicit recommendation to reorder **or keep the baseline**;
- a default **Crew route** with one numbered map, ordered stop cards, depot
  return time, and Google Maps/CSV hand-off;
- a separate **Planner details** view for method comparison, exact modeled
  temperatures, scoring assumptions, safety limits, and API source records;
- no certainty score until forecast reliability is empirically calibrated.

This keeps the professor-inspired reliability work as a defensible next layer
without presenting an authored confidence value as measured evidence.

## Hackathon positioning

- **Primary track:** Track 3 — Industrial & Enterprise
- **Technical overlap:** Track 5 — Model Designing
- **Initial customers:** utilities, telecom operators, construction firms,
  municipal public-works teams, field-maintenance companies, and last-mile
  operators
- **Initial U.S. demonstration geography:** a six-stop Phoenix corridor selected
  after validating API coverage and real-data quality

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
   from FortyGuard for each job location and time window. The implemented first
   step is a historical replay that maps each coordinate to its returned tile at
   three or more sampled hours.
2. Estimate heat exposure over each job and over the worker's full shift.
3. estimate how trustworthy each risk prediction is under the current input
   conditions.
4. Optimize job order and timing subject to travel, duration, priority,
   deadline, and uncertainty-aware heat-screening constraints.
5. Present one crew-ready route first, then keep the method comparison, exact
   temperatures, source evidence, and limitations in a separate planner view.

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

## Current MVP scope

The implemented real-data demo supports:

- Six fictional work orders at real Phoenix landmarks, with duration, priority,
  and time windows
- Ten hourly FortyGuard temperature heatmaps for the selected historical replay
  date, with strict rejection of missing or uncovered values
- A documented, configurable heat-exposure score
- Two planner-comparable schedules: a distance-efficient operations baseline
  and a point-temperature heat-aware schedule
- A route-first crew view with a clear keep/change decision, numbered map,
  ordered stop cards, first stop, depot return, Google Maps directions hand-off,
  and downloadable CSV run sheet
- A planner view with exact modeled temperatures, cumulative exposure, hot-work
  time, estimated travel, on-time completion, method comparison, scoring,
  source records, and safety boundaries
- No synthetic or substitute temperature profile and no certainty indicator
  before calibration evidence exists

The scheduler's real-data mode is now implemented for historical replay. It
uses real FortyGuard API output but does not call that output sensor ground
truth. Current-day/forecast collection and calibrated reliability remain the
next milestones.

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
- Performance under held-out temporal and geographic distribution shifts

## Three-minute demo story

1. A dispatcher chooses a historical Phoenix replay date and builds the route.
2. CertiRoute checks every job and its constraints against ten real FortyGuard
   heatmaps.
3. The default crew view gives one plain-language keep/change decision.
4. The crew follows large stop numbers on the map and matching ordered cards,
   then opens the same stop order in Google Maps or downloads the CSV run sheet.
5. A planner opens the secondary view to audit both methods, exact modeled
   temperatures, scoring assumptions, and FortyGuard source records.
6. The demo closes with the ambient-temperature safety boundary and the honest
   statement that forecast certainty remains unclaimed research work.

## Responsible-use boundary

CertiRoute is decision support, not medical advice. Heat risk depends on more
than ambient temperature, including humidity, solar radiation, workload,
clothing/PPE, acclimatization, health status, shade, and access to water and
rest. The UI and documentation must disclose which factors are and are not
modeled. Any occupational thresholds used in the prototype must be sourced and
configurable rather than silently hard-coded.

## Definition of hackathon success

The current project succeeds when a judge can replay the Phoenix workday and
understand the selected stop order at a glance, hand it to a crew, audit why it
was chosen, and verify that every temperature input came from FortyGuard. A
future customer-input version should preserve operational constraints, reduce
modeled exposure when the data supports a change, and communicate uncertainty
only after it is calibrated.
