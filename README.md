# CertiRoute

CertiRoute is a certainty-aware heat-risk scheduler for mobile outdoor crews.
It converts FortyGuard's hyperlocal temperature intelligence into job sequences
with lower modeled exposure instead of stopping at maps or threshold alerts.

The working product and research brief is in
[`docs/PROJECT_IDEA.md`](docs/PROJECT_IDEA.md).

## Stack

- Python 3.11–3.13 (Python 3.12 is used for local development)
- Streamlit for the hackathon dashboard
- `httpx` for the FortyGuard API integration
- Pure-Python beam-search scheduling (heavier solvers such as OR-Tools are
  deferred until scenario-based optimization requires them)
- PyDeck for maps and route schematics
- Pytest and Ruff for verification

## Repository layout

```text
app/                         Streamlit entry point
data/sample/                 Synthetic, non-sensitive demo inputs
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

The plan defaults to **FortyGuard API (real)**. A historical replay samples
three single-hour heatmaps by default, maps every job coordinate to its returned
temperature tile, and uses those values in the schedule. Before any missing
sample is submitted, the UI shows the exact task count and requires explicit
authorization. Completed responses are cached under Git-ignored `data/raw/` and
shown with activity IDs and collection timestamps.

An explicitly labelled synthetic fallback remains available for offline demos
and for exercising the future certainty-aware method. Real mode does not invent
a confidence score: until forecast/realization calibration is complete, its
certainty-aware plan intentionally coincides with the point-temperature
heat-aware plan.

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
