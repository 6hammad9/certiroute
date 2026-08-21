# Occupational Heat Model and Product Boundaries

Last reviewed against U.S. federal guidance on 2026-08-21.

## Product claim

CertiRoute provides forecast-based occupational heat-risk screening to support
route and shift planning. It identifies schedules with lower modeled exposure
under entered assumptions.

It does **not** determine that work is safe, certify OSHA compliance, or replace
an on-site effective-WBGT assessment, a site-specific heat illness prevention
plan, or qualified safety advice.

## Why ambient temperature is not enough

FortyGuard ambient temperature is valuable for hyperlocal advance planning, but
temperature alone cannot establish:

- Heat Index, because relative humidity or dew point is also required
- WBGT, because humidity, radiant heat, sunlight, and air movement matter
- metabolic heat from workload
- clothing and PPE heat burden
- acclimatization or individual susceptibility
- local worksite effects such as roofs, asphalt, reflected sunlight, trenches,
  blocked wind, machinery, and hot materials
- a safe work/rest schedule or compliance determination

OSHA explicitly warns that temperature is only one environmental factor and
that weather-station conditions may not match a worksite. See
[OSHA Heat Hazard Recognition](https://www.osha.gov/heat-exposure/hazards).

## Staged model

### 1. Advance planning screen

- Retrieve hourly hyperlocal ambient temperature.
- Add relative humidity/dew point through available environmental data.
- Calculate or retrieve Heat Index and label it as a shade/light-wind screening
  measure.
- Prefer forecast WBGT where an official source and suitable spatial resolution
  are available.
- Recalculate the route's screening exposure hour by hour.

The National Weather Service explains the different purposes and inputs of Heat
Index and WBGT in [WBGT versus Heat Index](https://www.weather.gov/ict/wbgt).

### 2. Worksite assessment inputs

For operational recommendations, collect or require:

- on-site WBGT near the worker and in the sun when work occurs in the sun
- workload category: light, moderate, heavy, or very heavy
- exact clothing/PPE ensemble and its adjustment
- acclimatization/new-or-returning status
- intended work/rest duration
- water, cool recovery area, supervision, communication, and emergency controls

Where workload is unknown, use the more conservative category. Where
acclimatization is unknown, treat the worker as unacclimatized. Do not infer
personal medical factors; present them only as private self-check prompts.

### 3. Effective-WBGT comparison

Apply the relevant clothing/PPE adjustment to measured WBGT, then compare the
effective WBGT with the appropriate NIOSH Recommended Alert Limit for
unacclimatized workers or Recommended Exposure Limit for acclimatized workers,
using workload and work/rest assumptions. These are recommendations designed to
protect most healthy workers, not guarantees for every person. See the
[NIOSH Occupational Exposure to Heat and Hot Environments criteria document](https://www.cdc.gov/niosh/docs/2016-106/default.html).

## Regulatory wording

As of the review date, the United States does not have one legally universal
numeric heat threshold. Federal OSHA generally relies on the General Duty Clause
for recognized serious heat hazards, while state-plan requirements vary. The
federal heat rule remains proposed rather than a final nationwide standard.

Sources:

- [OSHA Heat Standards](https://www.osha.gov/heat-exposure/standards)
- [OSHA Heat Rulemaking](https://www.osha.gov/heat-exposure/rulemaking)

Avoid these phrases:

- "safe route"
- "OSHA approved" or "OSHA compliant"
- "certified safe"
- "prevents heat illness"

Use this formulation:

> Lower forecast screening risk under the entered environmental, workload,
> clothing, and acclimatization assumptions.

## Emergency boundary

Symptoms override every score. Suspected heat stroke requires emergency action:
call 911, stay with the worker, move them to a cooler area, and begin rapid
cooling. See
[NIOSH heat-related illness first aid](https://www.cdc.gov/niosh/heat-stress/about/illnesses.html).
