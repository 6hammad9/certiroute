# CertiRoute

**[Open the live app](https://certiroute.streamlit.app)** &middot; no login, no install.
Reviewing a finished day runs entirely on measurements committed to this
repository, so it costs nothing and works on a cold deployment.

CertiRoute tells an outdoor crew **what time to start today**.

A dispatcher marks the crew base and work sites on a map. CertiRoute reads
today's street-level heat from FortyGuard, predicts the remaining hours from a
trained model of how that area's heat moves through a day, and compares every
start time the shift could use. It returns one decision - begin at this hour -
with the visit order already worked out and a calibrated interval attached.

**Why the start time and not the stop order.** Both levers were measured on
real FortyGuard data across Phoenix, Houston and Miami. Reordering stops inside
a fixed window changed the recommended sequence in zero of three cases: site-to
-site spread is 0.32-2.32 C while the swing across a day is 5.2-9.3 C, so *when*
a crew works dominates *what order* they work in by roughly an order of
magnitude. CertiRoute still computes the heat-aware order, and still reports it
- including when it changes nothing, which is most of the time.

## Does it work?

Each area model is graded on days after its training window closed, by
rebuilding the recommendation it would have made that morning from the
whole-day reading alone - never seeing an hourly value - and replaying every
candidate start on the temperatures the day actually produced.

| Area | Clean days graded | Picked the best start | Exposure avoided |
| --- | --- | --- | --- |
| Phoenix | 4 | 4 / 4 | 19.7-37.1% |
| Houston | 2 | 2 / 2 | 47.7-58.6% |
| Miami | 3 | 3 / 3 | 33.3-49.5% |

The same nine days planned **the evening before**, anchored on the previous
day's reading rather than the target day's, chose the same start every time -
9/9, zero regret. An error in the day's level shifts the whole curve without
reordering its hours, so the timing survives where the temperature does not.
That is why tomorrow can be planned tonight and today's temperatures cannot be
promised for it.

Zero regret on all nine: on no day would another start time have been cooler.
The spread in savings is not a difference in model quality but in local
climate - Houston and Miami have steeper afternoons than Phoenix - and it is
the reason each area carries its own trained model instead of one number being
asserted everywhere.

Prediction error, measured by rolling the origin forward over 16 consecutive
Phoenix days, is **0.79 C mean absolute error**. That figure includes the
11-14 Aug cool snap, when the daily level fell 34.9 -> 30.7 C and error rose to
2.2 C; on ordinary days it is 0.24-0.60 C. The model tracks a regime it has
seen and lags one that changes under it, and the interval is calibrated on a
window that includes the disturbance rather than one that avoids it.

Committed evidence: `data/evidence/recommendation_grades_*.json`.

## Deploying it

The app runs on Streamlit Community Cloud with no changes.

1. Push this repository to GitHub.
2. Point Streamlit Cloud at `app/main.py`. `requirements.txt` and
   `.python-version` are committed, so the build needs no configuration.
3. Add the API key under **Settings -> Secrets**:

   ```toml
   FORTYGUARD_API_KEY = "your-key"
   ```

   Streamlit exposes secrets as environment variables, which is where the app
   already reads it from. `.env` is for local work only and is never committed.

### What a deployed instance can do without spending a credit

`data/raw/` is 1.8 GB of whole heatmaps and is deliberately not committed. What
the product reads from them is the temperature at each work site, so those are
distilled into `data/evidence/measured_profiles.json` - the same measurements,
around 82 KB - and committed.

So a fresh deployment can, with no API call at all:

- play the landing showcase, a real day the model had never seen;
- review and grade any collected day against its measured hours.

Planning today or tomorrow needs one live reading, which is the only part that
spends credits. Rebuild the distilled file after collecting more days with:

```powershell
.\.venv\Scripts\python.exe scripts\build_measured_profiles.py
```

The working product and research brief is in
[`docs/PROJECT_IDEA.md`](docs/PROJECT_IDEA.md).

## Stack

- Python 3.11–3.13 (Python 3.12 is used for local development)
- Streamlit for the operator interface, with a hand-built visual system in
  `src/certiroute/theme.py` (Barlow Condensed / Barlow / IBM Plex Mono, inline
  SVG icons, no emoji)
- `httpx` for the FortyGuard API integration
- Pure-Python beam-search scheduling (heavier solvers such as OR-Tools are
  deferred until scenario-based optimization requires them)
- Folium and `streamlit-folium` for the click-to-place setup map
- PyDeck for the numbered route-result preview
- Pytest and Ruff for verification

## Repository layout

```text
app/                         Streamlit entry point
data/sample/                 Optional fictional Phoenix example work orders
data/climatology/            Committed trained area models (offsets + evidence)
docs/                        Product, research, and build documentation
notebooks/                   Exploration and model experiments
src/certiroute/
  climatology.py             Trained, versioned per-area diurnal heat model
  daily_level.py             Today's whole-day aggregate, one level per site
  same_day.py                Today's reading -> start-time decision -> grading
  forecasting.py             Diurnal shape learning and conformal calibration
  shift_timing.py            Exposure across candidate shift start times
  shift_planning.py          Recommend a start, then score it against reality
  theme.py                   Type, colour, spacing, and the inline icon set
  map_scenario.py            Guided map-selection state and default jobs
  map_picker.py              Clickable Folium map adapter
  job_manifest.py            Work-order validation and scenario fingerprinting
  fortyguard/                FortyGuard API adapter
  collection/                Secret-aware forecast/residual archive
  risk/                      Exposure and certainty calculations
  optimization/              Schedule/route optimization
  domain/                    Shared data models
tests/                       Automated tests
```

## Local setup

On Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e '.[dev]'
Copy-Item .env.example .env
# Add the FortyGuard key to .env, then:
.\.venv\Scripts\python.exe -m streamlit run app\main.py
```

The interface is one guided, real-data-only workflow:

1. Choose a nearby U.S. city to position the map, then pan or zoom wherever the
   crew actually works. The city is a starting view, not a boundary.
2. Click once for the mint crew start/return base, then click 2–9 orange work
   sites. Coordinates stay behind the interface.
3. Keep the ready-to-use defaults—45 minutes per site, priority 3, and the usual
   08:00–17:00 shift—or change names, durations, the day, and the shift. The
   shift you enter is the *baseline*: the hours you would work without heat
   planning. CertiRoute recommends against it.
4. Press **Plan today's shift**. Exactly one FortyGuard request is sent: today's
   whole-day reading for this area. The hour-by-hour shape is already trained
   and committed, so no fortnight of heatmaps is fetched at plan time.
5. Follow the default **Crew route**: the start time, every candidate start with
   its modelled exposure, a numbered route preview, ordered stop cards,
   depot-return time, and Google Maps/CSV hand-off. Open **Planner details** for
   the model's provenance, its held-out error, the interval width, and the
   ordering result.

### Two modes

**Planning today** is the default and the product. It needs a trained model for
the operating area; where none exists the app says so and refuses rather than
extrapolating.

**Reviewing a finished day** replays measured hourly temperatures for a past
date, and can grade the model on it: CertiRoute rebuilds the recommendation it
would have made that morning from the whole-day reading alone, then scores it
against the temperatures the day actually produced - what it chose, what
hindsight would have chosen, exposure genuinely avoided, and the regret between
them. Grading refuses on any day the model trained or calibrated on, because
that measures memory rather than skill.

### How today is predicted

FortyGuard exposes no hourly forecast, and its hourly mode (filter type 1)
returns nothing for the current date. Its whole-day aggregate (filter type 3)
*is* available for today. That single number per tile is therefore the only way
a plan made today can be conditioned on today at all.

So CertiRoute learns, offline from cached snapshots, how far each hour sits
above or below its own site's whole-day level, and ships those offsets as a
committed artifact per area. At plan time it reads today's aggregate and applies
them. Offsets are learned against per-site levels rather than an area mean,
matching exactly how they are applied, so a site that runs persistently hot is
not counted twice.

Day-ahead prediction is deliberately **not** offered. Predicting tomorrow's
level from past days measured 2.27 C mean absolute error with one day missed by
4.62 C - too loose to put a crew's morning on.

### What the interval means

Every plan carries a radius: a split-conformal quantile over scored days.
Whole days are the exchangeable unit, not individual readings, because a day
runs hot or cool as a whole; pooling site-hours would pretend to far more
independent evidence than exists.

Scores come from **rolling the origin forward** - each day is predicted using
only the days before it, which is also exactly how the product is used. A
single chronological holdout was tried first and rejected: it reported 0.91 C
for a split ending in a calm week and 1.21 C for one spanning a cool snap, for
the same method. Neither number described the method, and calibrating on the
calm window would have made the interval narrowest precisely when it needed to
be widest.

More scored days also support a tighter *claim*: sixteen scores support 90%
coverage where three support only 75%. Schedules are built against the top of
the interval, not its middle, so a hotter-than-predicted day still lands inside
the assumption the start time was chosen under. A uniform widening barely
reorders the candidates, so a cautious interval costs little decision quality -
Phoenix scored 4/4 optimal under a +/- 4.4 C radius.

### Training an area model

```powershell
.\.venv\Scripts\python.exe scripts\train_climatology.py --area phoenix --live
```

Hourly history is read from the local snapshot cache and costs no credits. The
only network calls are one whole-day aggregate per training day. The script
prints held-out error, error at sites left out of training, and the tightest
interval the held-out day count supports, then writes `data/climatology/`.

The map workflow creates ordinary work orders automatically, so a first-time
user does not need coordinates or a spreadsheet schema. The city selector only
positions the map: users can pan and choose any covered U.S. locations. When
stops do not fit within FortyGuard's 10 mi² per-request AOI limit, CertiRoute
partitions them into valid heat-data areas and combines the results
automatically.

CSV import is optional and lives under **Advanced: import work orders or load
the walkthrough**. It is intended for dispatchers who already export jobs from
another system. The app supplies a downloadable template and accepts UTF-8 CSV
files of 1 MB or less with 2–9 jobs. The required columns are:

```text
job_id,name,latitude,longitude,duration_minutes,priority,earliest_start,latest_finish
```

Times use 24-hour `HH:MM`; priority is 1–5. Extra export columns are accepted
but ignored. An import places the orange job markers; the dispatcher then
clicks the actual mint crew base. Coordinates are checked as WGS84 values;
FortyGuard enforces its U.S.-coverage boundary. Uploaded work orders are not
written to the repository.

Only missing FortyGuard snapshots are retrieved, and only after an explicit
button press. Completed responses are cached under Git-ignored
`data/raw/`, so an interrupted collection can resume from real API evidence.
The active result is keyed by the normalized job manifest, depot, date, shift,
granularity, and heatmap requests, preventing a changed scenario from reusing a
stale route. Generated or substitute temperatures never enter the heat score or
crew-route result.

The bundled Phoenix file remains useful for a one-click walkthrough, but it is
not the primary input or a data fallback. Its landmarks are real; its work
orders and constraints are fictional; its saved or newly collected temperature
evidence is real FortyGuard output.

The numbered in-app route is an order preview: its connecting lines and travel
times use straight-line distance at 25 km/h. CertiRoute currently chooses the
job order but is not a road-routing engine. **Open ordered stops in Google
Maps** hands that same order to Google for road navigation; the downloadable
run sheet provides the same sequence for another dispatch system.

CertiRoute plans the current day and reviews finished ones. It does not offer
day-ahead forecasts, for the measured reason given above. The per-reading
`certainty` field FortyGuard returns is a documented no-penalty sentinel, not a
probability, so it is never used as one: uncertainty is expressed only through
the calibrated interval, which is measured on held-out days.

The research papers that motivated this work - prospect certainty for
data-driven models, and chance-constrained back-mapping - are an *inspiration*,
not an implementation. Both require access to model internals or training-label
distributions that a black-box vendor API does not expose. CertiRoute uses
split-conformal calibration instead, which needs only its own predictions and
their residuals.

Never commit `.env` or an API key.

To inspect the exact real-data plan without submitting anything:

```powershell
.\.venv\Scripts\python.exe scripts\collect_real_demo.py
```

The script prints cache hits and the missing task count. A live collection also
requires `--live` and an explicit `--max-new-tasks N` hard cap. Add
`--verify-status` to compare cached payloads with read-only activity-status GETs;
it does not submit a new heatmap.

## Verification

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m pytest -q
```

See [`docs/API_INTEGRATION.md`](docs/API_INTEGRATION.md) for the verified API
contract and current unknowns. See [`docs/SAFETY_MODEL.md`](docs/SAFETY_MODEL.md)
for the product's occupational-heat boundaries and planned risk model. The
black-box calibration claim and evidence plan are in
[`docs/RELIABILITY_METHOD.md`](docs/RELIABILITY_METHOD.md).
