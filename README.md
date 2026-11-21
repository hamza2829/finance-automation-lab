# finance-automation-lab

[![CI](https://github.com/hamza2829/finance-automation-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/hamza2829/finance-automation-lab/actions/workflows/ci.yml)

A self-contained lab that automates a **supplier invoice approval process**:
Power Automate style flow definitions, a mock accounting and approval API,
an AI classification step with a rule fallback and human review, a monthly
Power Query / Excel report, and a structured error analysis of the data flow.

Everything runs with `pip install` and Python. No Docker, no API keys, no
accounts, no calls to Microsoft or any LLM provider.

## Honest limits

Read this first.

* **No real Power Automate run.** The three flows in `flows/` are JSON definitions checked by a static
  validator. They were never imported into a tenant or executed. `docs/power-automate-import.md`
  describes how one would do it; that is marked *not done in this repo*.
* **No Copilot, no real LLM.** The AI step calls a configurable URL. In tests, CI and the demo data that
  URL is answered by `ai/mock_model.py`, a **deterministic keyword mock**, not a model. Its "accuracy"
  in the error report (99.2 %) only demonstrates the measurement; it says nothing about a real model.
* **No real Power Query run.** The M scripts in `powerquery/queries/` are not executed (CI runs on Linux,
  without Excel). CI compares the SQL numbers with a **step-by-step Python port** of the M logic on the
  same CSV export. A mistake that is in both the M script and its port would not be caught.
* **Generated data.** 300 invoice documents (Aug-Oct 2026) from `app/seed.py`, with deliberately injected
  anomalies (duplicates, amount mismatches, negative amounts, a misconfigured interface mapping, API
  faults, an LLM outage). Vendors, people and e-mail addresses are fictional (`example.com`).
* **The accounting system is a mock** inside the same FastAPI app. No authentication, single SQLite file,
  not built for concurrency.
* **Prompt injection is only partly mitigated.** Invoice text goes into the prompt. The answer is
  validated against the allowed categories / cost centers, so injected text can at most pick a wrong
  *valid* label with a high confidence. Human review below the threshold does not catch that case.
* Escalation counts calendar hours (3 × 24 h), not business days.

## What CI checks (and the real numbers)

| Check | Command | Result on `main` |
|---|---|---|
| Flow validator | `python -m flows.validate_flows --check-api` | 3 flows, 0 errors |
| Tests + coverage | `python -m pytest --cov --cov-fail-under=85` | **149 tests** passed (134 test functions, some parametrised), 93 % line coverage |
| Report comparison | `python -m powerquery.compare_report` | SQL == M port == Excel == committed snapshot |
| Error analysis up to date | `python -m analysis.error_analysis && git diff --exit-code analysis/` | no diff |

Tests per file:

| File | Tests | Covers |
|---|---:|---|
| `tests/test_workflow.py` | 44 | normalisation, amounts, duplicates, negative amounts, approval rights, escalation at 72 h, idempotent booking, period close |
| `tests/test_classifier.py` | 27 | LLM success, timeout, HTTP 500, connection error, invalid answers, low confidence, threshold boundary, env config |
| `tests/test_flows.py` | 27 | the 3 real flows, broken flows: missing trigger, cycles, dead branches, run order, unknown routes |
| `tests/test_api.py` | 15 | REST endpoints end to end with TestClient |
| `tests/test_report.py` | 13 | SQL vs M port for 3 months, Excel round trip, snapshot, empty month |
| `tests/test_sync.py` | 9 | retries, timeout after commit, parking on 4xx, fault plan |
| `tests/test_seed.py` | 7 | 300 documents, determinism, consistency of the generated timeline |
| `tests/test_analysis.py` | 7 | counts match injected anomalies, committed report is current |
| **Total** | **149** | |

These counts are from `pytest --collect-only` at the time of writing; the CI job summary shows the
numbers of each run.

## Results on the demo data

Monthly report October 2026 (`powerquery/expected/report_2026-10.json`):

| KPI | Value |
|---|---|
| Open invoices at month end | 8 (27,781.31 EUR): 5 pending approval, 2 escalated, 1 needs review |
| Approved in October | 79, average 54.44 h, median 31.62 h, 75.9 % within 3 days |
| Escalated (of the approved) | 18 |
| Rejected in October | 12 (80,452.87 EUR) |

Error analysis (`analysis/error_report.md`, generated): transient booking failures recovered by retry,
timeouts after commit absorbed by the idempotent endpoint, bookings parked because the posting period was
closed, ledger amounts off by the VAT for one vendor with a `net` mapping, duplicates caught at intake
(half of them only thanks to number normalisation), and re-issued invoices that slipped through and were
approved twice. Each with count, amount, root cause and fix.

## Quick start

```bash
pip install -r requirements.txt
```

```bash
python -m app.seed
```

```bash
python -m pytest --cov
```

```bash
python -m flows.validate_flows --check-api
```

```bash
python -m powerquery.compare_report
```

```bash
python -m analysis.error_analysis
```

Run the API locally (OpenAPI UI at `http://127.0.0.1:8000/docs`):

```bash
uvicorn app.main:create_app --factory --port 8000
```

Use the mock LLM over HTTP instead of in-process:

```bash
uvicorn ai.mock_model:app --port 8100
```

```bash
FAL_LLM_URL=http://127.0.0.1:8100/v1/classify uvicorn app.main:create_app --factory --port 8000
```

| Variable | Default | Meaning |
|---|---|---|
| `FAL_DB_PATH` | `data/finance.db` | SQLite file |
| `FAL_LLM_URL` | unset (rules only) | classifier endpoint |
| `FAL_LLM_TIMEOUT` | `5` | seconds |
| `FAL_CONFIDENCE_THRESHOLD` | `0.75` | below → human review |
| `FAL_OUTPUT_DIR` | `powerquery/output` | Excel output |
| `FAL_EXPORT_DIR` | `data/export` | CSV export for Power Query |

## Repository map

```
flows/        3 flow definitions (JSON) + validate_flows.py
app/          FastAPI mock (main.py), business rules (workflow.py), SQLite schema (db.py),
              booking interface with fault injection (sync.py), demo data (seed.py)
ai/           classifier.py (LLM URL + validation + fallback), rules.py, mock_model.py
powerquery/   queries/*.pq (M), report.py (SQL + Excel), m_port.py, compare_report.py, expected/
analysis/     error_analysis.py, error_report.md, error_report.json
tests/        pytest suite
docs/         process.md, interfaces.md, architecture.md, power-automate-import.md
```

## Docs

* [Process description](docs/process.md): roles, steps, status model, business rules
* [Interfaces](docs/interfaces.md): data flows between finance and IT, REST endpoints
* [Architecture](docs/architecture.md): Mermaid diagrams, design decisions
* [Importing the flows into Power Automate](docs/power-automate-import.md): **not done in this repo**
* [Error analysis report](analysis/error_report.md)
