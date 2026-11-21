# Architecture

Dashed boxes are **not part of this repository** (they exist only as JSON
definitions or as documentation). Everything else runs in CI.

```mermaid
flowchart TB
    subgraph M365["Microsoft 365 / Power Automate (not run here)"]
        direction TB
        F1["Flow 1<br/>Invoice received"]
        F2["Flow 2<br/>Approval by cost center manager"]
        F3["Flow 3<br/>Escalation after 3 days<br/>(daily 07:00)"]
        APPR["Approvals connector"]
        MAIL["Outlook / Teams"]
    end

    subgraph REPO["This repository (runs in CI)"]
        direction TB
        API["FastAPI mock<br/>app/main.py"]
        WF["Business rules<br/>app/workflow.py"]
        DB[("SQLite<br/>invoices, approvals,<br/>cost_centers, ledger,<br/>api_call_log")]
        CLS["Classifier<br/>ai/classifier.py"]
        RULES["Rules fallback<br/>ai/rules.py"]
        MOCK["Mock LLM<br/>ai/mock_model.py"]
        SYNC["Booking interface<br/>app/sync.py"]
        SEED["Demo data generator<br/>app/seed.py"]
        VAL["Flow validator<br/>flows/validate_flows.py"]
        RPT["Report SQL + Excel<br/>powerquery/report.py"]
        PORT["M logic port<br/>powerquery/m_port.py"]
        ANA["Error analysis<br/>analysis/error_analysis.py"]
    end

    PQ["Power Query M scripts<br/>powerquery/queries/*.pq<br/>(Excel, not run here)"]
    LLM["Real LLM endpoint<br/>(not called)"]

    F1 -- "REST/JSON" --> API
    F2 -- "REST/JSON" --> API
    F3 -- "REST/JSON" --> API
    F2 --> APPR
    F1 --> MAIL
    F3 --> MAIL
    API --> WF --> DB
    WF --> CLS
    CLS -- "FAL_LLM_URL" --> MOCK
    CLS -. "FAL_LLM_URL (optional)" .-> LLM
    CLS -- "timeout / error / invalid" --> RULES
    SYNC -- "POST /accounting/bookings<br/>(fault injection)" --> API
    SEED --> WF
    SEED --> SYNC
    DB -- "CSV export" --> PQ
    DB -- "CSV export" --> PORT
    DB --> RPT
    RPT -- "compare" --- PORT
    DB --> ANA
    VAL -. "checks" .-> F1
    VAL -. "checks routes" .-> API

    classDef ext stroke-dasharray: 5 5
    class F1,F2,F3,APPR,MAIL,PQ,LLM ext
```

## Happy path

```mermaid
sequenceDiagram
    autonumber
    participant OCR as Inbox / OCR
    participant F1 as Flow 1
    participant API as Approval API
    participant LLM as LLM (mock)
    participant F2 as Flow 2
    participant MGR as Cost center manager
    participant ACC as Accounting (mock)

    OCR->>F1: invoice JSON
    F1->>API: POST /invoices
    API-->>F1: 201 status=received
    F1->>API: POST /invoices/{id}/classify
    API->>LLM: prompt + text
    LLM-->>API: category, cost_center, confidence 0.86
    API-->>F1: needs_human_review=false
    F1->>API: POST /invoices/{id}/approvals
    F1->>F2: invoice_id, approval_id
    F2->>MGR: Start and wait for an approval
    MGR-->>F2: Approve
    F2->>API: POST /invoices/{id}/approve
    Note over API,ACC: nightly 22:00
    API->>ACC: POST /accounting/bookings (idempotent)
    ACC-->>API: 201 ledger entry
```

## Design decisions

* **Integer cents, naive UTC strings.** No float drift; ISO strings sort and export cleanly to CSV/Power Query.
* **Business rules outside the HTTP layer.** `app/workflow.py` takes an explicit `now`, so the demo
  generator replays a 3-month timeline through the same rules the API enforces, and tests can pin time.
* **Store every document.** Duplicates and invalid documents are kept with a status and reason instead of
  being rejected with 4xx, so the error analysis can count them and nothing is lost silently.
* **Idempotent booking.** One ledger entry per invoice. A retry after a lost response returns 200 instead
  of booking twice.
* **LLM behind a validated contract.** The model may only answer with known categories / cost centers and
  a confidence in 0..1; anything else is treated as a failure and the rules take over. A prompt injection
  in invoice text can therefore at most pick a wrong *valid* label; the confidence threshold and human
  review are the safety net (see README limits).
* **Determinism.** `random.Random(2026)` plus SHA-256 for all "random" behaviour (mock model jitter, fault
  injection). CI regenerates the report and the error analysis and fails if they differ from the commit.
