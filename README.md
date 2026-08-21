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

The live tab never submits automatically. Its button explicitly warns that a
successfully completed API task can consume credits.

Never commit `.env` or an API key.

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
