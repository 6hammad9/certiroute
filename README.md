# CertiRoute

CertiRoute is a heat-aware scheduler for mobile outdoor crews. A dispatcher
marks the crew base and work sites directly on a map; CertiRoute converts
FortyGuard's hyperlocal temperature intelligence into an operational decision:
keep the distance-efficient work order, or change it when the modeled exposure
benefit justifies the travel trade-off.

The working product and research brief is in
[`docs/PROJECT_IDEA.md`](docs/PROJECT_IDEA.md).

## Stack

- Python 3.11–3.13 (Python 3.12 is used for local development)
- Streamlit for the hackathon dashboard
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
docs/                        Product, research, and build documentation
notebooks/                   Exploration and model experiments
src/certiroute/
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
3. Keep the ready-to-use defaults—45 minutes per site, priority 3, and each job
   available for the full 08:00–17:00 shift—or optionally change names,
   durations, the completed replay day, and the same-day shift.
4. Press **Create my heat-aware route**. Nothing is submitted until this
   explicit action, and CertiRoute first proves that a complete depot-to-depot
   schedule is feasible.
5. Follow the default **Crew route**: one decision, one numbered route preview,
   an ordered stop card for each job, depot-return time, and Google Maps/CSV
   hand-off. Open **Planner details** only when the comparison, exact modeled
   temperatures, scoring assumptions, safety limits, or API source records are
   needed.

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

Only missing FortyGuard snapshots are retrieved, and only after **Create my
heat-aware route** is clicked. Completed responses are cached under Git-ignored
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

This version plans completed historical replays only. Current-day routing and
forecasts up to 12 hours ahead are deferred until FortyGuard's request-time-zone
semantics are documented and verified. Forecast reliability remains a research
layer rather than a current UI claim. Until forecast-versus-realization
calibration is complete, CertiRoute does not display or optimize a certainty
score in its real-data result.

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
