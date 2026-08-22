# CertiRoute

CertiRoute is a heat-aware scheduler for mobile outdoor crews. It converts
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
- PyDeck for the numbered route map
- Pytest and Ruff for verification

## Repository layout

```text
app/                         Streamlit entry point
data/sample/                 Fictional, non-sensitive demo work orders
docs/                        Product, research, and build documentation
notebooks/                   Exploration and model experiments
src/certiroute/
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

The interface is one guided, real-data-only page with two deliberately separate
views:

1. Review six fictional work orders at real Phoenix landmarks.
2. Build a historical replay from ten hourly, per-site FortyGuard heatmaps.
3. Follow the default **Crew route**: one decision, one numbered map, an ordered
   stop card for each job, depot-return time, and Google Maps/CSV hand-off.
4. Open **Planner details** when the scheduling comparison, exact modeled
   temperatures, scoring assumptions, safety limits, or API source records are
   needed.

The default replay covers one 9.48 mi² Phoenix service area at 60 m tile
granularity. Missing hours are retrieved only after **Build crew route**
is clicked. Completed responses are cached under Git-ignored `data/raw/`, so an
interrupted collection resumes and a prepared demonstration reloads from real
API evidence. The product interface never generates substitute temperature
profiles.

Forecast reliability remains a research layer rather than a current UI claim.
Until forecast-versus-realization calibration is complete, CertiRoute does not
display or optimize a certainty score in its real-data result.

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
