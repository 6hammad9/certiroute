# CertiRoute: Main Project Idea

## One-sentence pitch

CertiRoute uses FortyGuard's street-level temperature intelligence to decide
whether a mobile crew's work order should change, reducing modeled cumulative
heat exposure when the benefit justifies the travel cost while preserving job
duration, priority, deadlines, and depot return.

## Current product slice

The working interface is deliberately narrower than the full research vision:

- a primary map-first flow: position the map near a U.S. city, pan wherever the
  crew works, click the crew base, then click 2–9 work sites without coordinates;
- automatic 45-minute, priority-3 jobs available for the full selected shift,
  with names, durations, completed replay day, and shift exposed only as
  optional changes;
- selection, work-order, shift, depot, and feasibility validation before any API
  submission, with automatic clustering into 10 mi²-or-smaller heat-data AOIs;
- hourly FortyGuard snapshots for a completed historical replay, with no
  substitute temperature data;
- one distance-efficient operations baseline and one heat-aware recommendation;
- an explicit recommendation to reorder **or keep the baseline**;
- a default **Crew route** with one numbered map, ordered stop cards, depot
  return time, and Google Maps/CSV hand-off;
- an honest separation between the straight-line route-order preview and Google
  Maps road navigation—the current scheduler does not claim road-network
  optimization;
- a separate **Planner details** view for method comparison, exact modeled
  temperatures, scoring assumptions, safety limits, and API source records;
- an advanced CSV import for customers who already export structured work
  orders, with a downloadable template rather than a required onboarding task;
- an explicitly optional Phoenix example with fictional work orders at real
  landmarks and real saved or API-retrieved FortyGuard temperatures;
- no certainty score until forecast reliability is empirically calibrated.

This keeps the professor-inspired reliability work as a defensible next layer
without presenting an authored confidence value as measured evidence.

## Hackathon positioning

- **Primary track:** Track 3 — Industrial & Enterprise
- **Technical overlap:** Track 5 — Model Designing
- **Initial customers:** utilities, telecom operators, construction firms,
  municipal public-works teams, field-maintenance companies, and last-mile
  operators
- **Customer input:** a compact U.S. service zone selected directly on a map;
  optional CSV import supports existing dispatch-system exports
- **Optional demonstration geography:** a six-stop Phoenix corridor selected
  after validating API coverage and real-data quality

The commercial product is an operational planning tool. The planned certainty
model is its research differentiator, rather than the product being presented
only as a forecasting experiment.

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

Given a dispatcher and a map, the implemented product:

1. Positions the map from a friendly U.S. city preset; the first click sets the
   crew's blue start/return base and the next 2–9 clicks create orange work
   sites.
2. Supplies usable job and workday defaults, while allowing optional names,
   durations, completed replay date, and same-day shift changes. Advanced users
   can instead import a validated CSV and then click the actual crew base.
3. Validates the selection, partitions spread-out jobs into valid heat-data
   areas, then waits for the explicit **Create my heat-aware route** action.
4. Proves that at least one depot-to-depot order satisfies the job windows
   before it can submit a FortyGuard request.
5. Loads saved evidence and collects only missing real FortyGuard snapshots,
   then maps each job coordinate to the returned temperature tiles.
6. Estimates heat exposure over each job and the worker's full shift, and
   optimizes job order and timing subject to straight-line travel estimates,
   duration, priority, deadlines, and modeled heat exposure.
7. Presents one crew-ready order first, while keeping method comparison, exact
   temperatures, source evidence, and limitations in **Planner details**. The
   same ordered stops can be opened in Google Maps for road navigation.

The future reliability layer will estimate how trustworthy each prediction is
under distribution shift and incorporate calibrated uncertainty into planning.
A future certainty-aware output might read:

> Move Site C from 13:10 to 08:40. The optimized schedule is expected to reduce
> cumulative heat exposure by 28% while adding 11 minutes of travel. Prediction
> certainty is high. Site D remains uncertain and is flagged for supervisor
> review.

## What makes it different

Most heat applications follow this pattern:

> Read temperature → display a map → issue a threshold alert.

The complete CertiRoute research direction follows a different pattern:

> Forecast exposure → assess whether the estimate is trustworthy → optimize an
> operational decision under uncertainty → quantify the avoided risk.

The project combines five elements:

- Hyperlocal, time-dependent temperature intelligence
- Cumulative exposure rather than only instantaneous thresholds
- Planned, empirically calibrated prediction certainty and distribution-shift
  awareness
- Constrained route and schedule optimization
- A measurable modeled-exposure-versus-efficiency business outcome

The current product claim is measurable heat-aware scheduling with real source
evidence. The research thesis goes further: **plan conservatively when the
heat-risk estimates themselves may be uncertain**. CertiRoute does not claim
that second capability is implemented yet.

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

The implemented real-data product supports:

- A guided map-first setup with a U.S. city preset, a first click for the crew
  start/return base, and 2–9 subsequent clicks for work sites
- Default map-created work orders named `Work site N`, each with a 45-minute
  duration, priority 3, and availability across the selected shift
- Optional editing of job names and durations; the primary path does not expose
  coordinates, priority, or per-job time windows
- A default completed historical replay of yesterday and an 08:00–17:00 shift,
  with optional replay-day and same-day shift changes up to 12 hours
- An advanced UTF-8 CSV import of 1 MB or less for 2–9 existing work orders,
  backed by a downloadable template with these required columns (unrelated
  export columns are ignored):
  `job_id,name,latitude,longitude,duration_minutes,priority,earliest_start,latest_finish`
- Automatic partitioning into independently validated heat-data AOIs of at most
  10 mi² per FortyGuard request; this is not presented as a user selection radius
- WGS84 coordinate validation plus a clear U.S.-only coverage requirement;
  FortyGuard remains the authority that enforces its coverage boundary
- Preflight route-feasibility validation before any API submission
- Hourly FortyGuard temperature snapshots for the selected shift, with strict
  rejection of missing or uncovered values and collection of only missing
  snapshots
- A scenario fingerprint covering the normalized manifest, depot, date, shift,
  granularity, and heatmap requests so changed inputs cannot display stale
  results
- A documented, configurable heat-exposure score
- Two planner-comparable schedules: a distance-efficient operations baseline
  and a point-temperature heat-aware schedule
- A route-first crew view with a clear keep/change decision, numbered map,
  ordered stop cards, first stop, depot return, Google Maps directions hand-off,
  and downloadable CSV run sheet
- Straight-line connecting lines and travel estimates at 25 km/h in the in-app
  preview; Google Maps supplies road directions for the preserved stop order,
  so the MVP does not claim to optimize against the road network
- A planner view with exact modeled temperatures, cumulative exposure, hot-work
  time, estimated travel, on-time completion, method comparison, scoring,
  source records, and safety boundaries
- An optional Phoenix example whose landmarks are real, whose work orders are
  fictional, and whose temperature evidence remains real FortyGuard output
- No synthetic or substitute temperature in heat scoring or the route result,
  and no certainty indicator before calibration evidence exists

The scheduler's real-data mode is now implemented for historical replay. It
uses real FortyGuard API output but does not call that output sensor ground
truth. Current-day and up-to-12-hour forecast collection remain deferred until
the API request-time-zone semantics are documented and verified. Calibrated
reliability remains a later research milestone.

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

1. A dispatcher positions the map near a U.S. city, pans wherever needed,
   clicks once for the blue crew base, and clicks 2–9 orange work sites; no
   coordinates or CSV are required.
2. The app shows ready defaults and a selected-work summary. Optional controls
   can name jobs, change visit duration, or change the completed replay day and
   shift; advanced CSV import and the Phoenix walkthrough stay collapsed.
3. The dispatcher presses **Create my heat-aware route**. CertiRoute validates
   the jobs, depot, shift, per-request heat-data areas, and route feasibility
   before any API submission.
4. The app reuses saved evidence, collects only missing real FortyGuard
   snapshots, and never substitutes temperatures.
5. The default crew view gives one plain-language keep/change decision. The crew
   follows large stop numbers on the straight-line order preview and matching
   cards, then opens the same stop order in Google Maps for road navigation or
   downloads the CSV run sheet.
6. A planner opens the secondary view to audit both methods, exact modeled
   temperatures, scoring assumptions, and FortyGuard source records.
7. The demo closes with the ambient-temperature safety boundary and the honest
   statement that forecast certainty remains unclaimed research work.

## Responsible-use boundary

CertiRoute is decision support, not medical advice. Heat risk depends on more
than ambient temperature, including humidity, solar radiation, workload,
clothing/PPE, acclimatization, health status, shade, and access to water and
rest. The UI and documentation must disclose which factors are and are not
modeled. Any occupational thresholds used in the prototype must be sourced and
configurable rather than silently hard-coded.

## Definition of hackathon success

The project succeeds when a first-time dispatcher can choose an area, click the
base and work sites, and build a feasible route from real FortyGuard evidence
without learning coordinates or a CSV schema. A judge should understand the
selected stop order at a glance, distinguish the straight-line preview from
road navigation, hand the order to a crew, audit why it was chosen, and verify
every temperature source record. Advanced CSV import must remain available for
real dispatch exports, and the optional Phoenix example must provide the same
complete path without being mistaken for customer data. Certainty is
communicated only after it is empirically calibrated.
