# Interfaces between finance and IT

All interfaces are REST/JSON unless noted. "Mock" means the counterpart is
simulated in this repository. Amounts are integer cents, timestamps are UTC
ISO-8601 without offset.

## Data flows

| ID | From → To | Protocol / format | Trigger / frequency | Payload (main fields) | Error handling | Business owner | Technical owner |
|---|---|---|---|---|---|---|---|
| IF-01 | Invoice inbox / OCR → Flow 1 | HTTP request trigger, JSON | per document | `invoice_number, vendor_id, description, net_cents, tax_cents, gross_cents, invoice_date, document_type, currency` | trigger schema validation | AP | IT |
| IF-02 | Flow 1 → Approval API `POST /invoices` | REST, JSON | per document | as IF-01 | `Intake_failed` mail + run failed on 4xx/5xx/timeout | AP | IT |
| IF-03 | Approval API → LLM endpoint (`FAL_LLM_URL`) | REST, JSON | per invoice | request: `model, prompt, text, vendor, allowed_categories, allowed_cost_centers`; response: `category, cost_center, confidence, rationale` | timeout (`FAL_LLM_TIMEOUT`, default 5 s), HTTP error, invalid answer → rule fallback | Finance controlling | IT / data |
| IF-04 | Flow 1 → Teams AP channel | Teams connector | confidence < threshold | invoice id, confidence, threshold | none (notification) | AP | IT |
| IF-05 | Flow 1 / Flow 3 → Flow 2 | HTTP request trigger, JSON | per approval | `invoice_id, approval_id` | caller run fails | Finance | IT |
| IF-06 | Flow 2 → Approvals connector | Power Automate Approvals | per approval | title, assignedTo (approver e-mail), details | wait; outcome Approve/Reject | Cost center manager | IT |
| IF-07 | Flow 2 → Approval API `POST /invoices/{id}/approve\|reject` | REST, JSON | per decision | `approver, comment` | 403 (not assigned, e.g. after escalation), 409 (already decided), 422 (reject w/o comment) → `Decision_not_recorded` mail | Cost center manager | IT |
| IF-08 | Flow 3 → Approval API `POST /approvals/escalate-overdue` | REST, JSON | daily 07:00 W. Europe | query `days=3`; response: list of new level-2 approvals | retry policy (3×, exponential), then IT alert | Finance lead | IT |
| IF-09 | Booking interface → Accounting `POST /accounting/bookings` (mock) | REST, JSON | nightly 22:00 | `invoice_id, amount_cents, posting_date` | timeout/5xx: 3 attempts (0/30/120 s); 4xx: parked, no retry; idempotent per invoice | Accounting | IT |
| IF-10 | Database → CSV export → Power Query → Excel | CSV (UTF-8), M scripts | monthly | `invoices.csv`, `approvals.csv` (columns in `powerquery/export_csv.py`) | CI compares SQL vs M-port vs Excel vs snapshot | Controlling | IT |
| IF-11 | Call log → error analysis | SQLite table `api_call_log` | nightly / on demand | `ts, method, endpoint, invoice_id, attempt, status_code, outcome, error` | n/a | Finance + IT | IT |

## REST endpoints of the mock (`app/main.py`)

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| GET | `/health` | liveness | 200 | |
| GET | `/cost-centers`, `/cost-centers/{id}` | master data | 200 | 404 |
| GET | `/invoices?status=&cost_center_id=&limit=&offset=` | list | 200 `{total, items}` | 422 |
| GET | `/invoices/duplicates?vendor_id=&invoice_number=` | duplicate check | 200 `{is_duplicate, duplicate_of, normalized}` | |
| GET | `/invoices/{id}` | detail incl. approvals | 200 | 404 |
| POST | `/invoices` | intake (stores every document) | 201 `{invoice, issues}` | 422 unknown vendor / schema |
| POST | `/invoices/{id}/classify` | AI step | 200 `{invoice, needs_human_review, threshold}` | 404, 409 |
| POST | `/invoices/{id}/review` | human classification | 200 | 409, 422 |
| POST | `/invoices/{id}/approvals` | start level-1 approval | 201 | 409 |
| POST | `/invoices/{id}/approve` | decision | 200 | 403, 409 |
| POST | `/invoices/{id}/reject` | decision (comment required) | 200 | 403, 409, 422 |
| GET | `/approvals/{id}` | approval detail | 200 | 404 |
| GET | `/approvals/overdue?days=3&as_of=` | overdue level-1 approvals | 200 | |
| POST | `/approvals/escalate-overdue?days=3&as_of=` | escalate all overdue | 200 list | |
| POST | `/approvals/{id}/escalate` | escalate one | 200 | 404, 409 |
| POST | `/accounting/bookings` | accounting mock, idempotent | 201 created / 200 existing | 409 not approved, 422 `period_closed` |
| GET | `/accounting/ledger?invoice_id=` | ledger entries | 200 | |

Error body: `{"error": "<code>", "message": "...", "details": {...}}`.
OpenAPI: run the app and open `/docs`. The flow validator (`--check-api`) checks
that every Http action in `flows/*.json` targets one of these routes.
