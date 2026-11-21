"""Generate the demo database: 300 invoice documents, Aug-Oct 2026.

Fully deterministic (``random.Random(SEED)`` and SHA-256, never ``hash()``),
so CI and every laptop produce byte-identical numbers.

The generator does not write final states directly. It replays a simulated
timeline through the same business rules the API uses:

1. intake       ``workflow.receive_invoice``      (duplicates, amount checks)
2. AI step      ``workflow.classify_invoice``     (mock LLM, rules fallback on outage days)
3. human review ``workflow.review_classification`` for low-confidence cases
4. approval     ``workflow.request_approval`` / ``decide`` / ``escalate``
5. booking      nightly ``sync.run_booking_sync`` over HTTP against the FastAPI
                app (in-process TestClient) with injected faults

Injected anomalies (for the error analysis) are listed in ``INJECTED``.

Usage:  python -m app.seed [--db data/finance.db]
"""
from __future__ import annotations

import argparse
import random
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from ai.classifier import ClassifierConfig, InvoiceClassifier
from ai.mock_model import MOCK_URL, mock_transport
from app import workflow as wf
from app.db import DEFAULT_DB_PATH, connect, reset, ts
from app.reference import CATEGORIES, COST_CENTERS, FINANCE_LEAD, VENDORS, default_cost_center
from app.sync import FaultInjectingClient, FaultPlan, run_booking_sync

SEED = 2026
N_DOCUMENTS = 300
START = date(2026, 8, 1)
END = date(2026, 10, 31)
SNAPSHOT_AT = datetime(2026, 11, 1, 6, 0)     # "now" of the generated snapshot
LLM_OUTAGE_DAYS = (date(2026, 9, 15), date(2026, 10, 20))
ESCALATION_RUN = time(7, 0)                   # escalation flow recurrence
BOOKING_RUN = time(22, 0)                     # nightly booking interface

INJECTED = {
    "exact_or_formatted_duplicates": 8,   # same vendor + same number (maybe re-formatted)
    "suspected_duplicates": 4,            # re-issued with a new number, same amount
    "header_amount_mismatch": 5,          # net + tax != gross
    "negative_amount": 3,                 # credit note sent as invoice
    "unsupported_currency": 2,            # USD document
}

TEMPLATES: dict[str, tuple[str, ...]] = {
    "IT_SOFTWARE": ("Annual licence renewal office suite, {n} seats", "Cloud hosting {month}, {n} vCPU",
                    "SaaS subscription CRM, {n} users", "Backup storage subscription {month}",
                    "Antivirus licences {n} seats"),
    "IT_HARDWARE": ("Laptops {n}x 14 inch incl. docking station", "Monitors 27 inch, {n} pcs",
                    "Network switch 24 port", "Headsets for call center, {n} pcs",
                    "Replacement keyboards and mice"),
    "CONSULTING": ("Consulting days process analysis ({n} days)", "Year-end audit support",
                   "Data analytics project milestone {n}", "Tax advisory {month}",
                   "Interim controller support {n} days"),
    "MARKETING": ("Online ad campaign {month}", "Trade fair booth construction",
                  "Flyers and brochures print run {n}", "Social media content package {month}",
                  "Sponsoring local sports event"),
    "TRAVEL": ("Flight FRA-BER for sales meeting", "Hotel {n} nights customer visit",
               "Train tickets for team offsite", "Rental car {n} days", "Taxi receipts {month}"),
    "OFFICE_SUPPLIES": ("Copy paper A4, {n} boxes", "Toner cartridges for printers",
                        "Office supplies {month}", "Notebooks and pens", "Desk organizers and folders"),
    "FACILITIES": ("Office cleaning {month}", "Security service night shifts {month}",
                   "Freight forwarding {n} pallets", "Waste disposal {month}",
                   "Maintenance of air conditioning"),
    "TRAINING": ("Excel advanced workshop, {n} participants", "Leadership seminar",
                 "Online course licences compliance training", "First aid training",
                 "Language course German B2"),
}
AMBIGUOUS = ("Services as agreed, see attachment", "Monthly flat fee per framework contract",
             "Miscellaneous items {month}", "Project support as discussed", "Reimbursement of expenses")
NET_RANGE_EUR = {
    "IT_SOFTWARE": (200, 15000), "IT_HARDWARE": (300, 8000), "CONSULTING": (1500, 25000),
    "MARKETING": (500, 20000), "TRAVEL": (80, 2500), "OFFICE_SUPPLIES": (30, 800),
    "FACILITIES": (400, 6000), "TRAINING": (200, 5000),
}
REJECT_REASONS = ("Not ordered by our team", "Price differs from purchase order",
                  "Service not yet delivered", "Wrong cost center, please re-route")


@dataclass
class Doc:
    doc: wf.InvoiceIn
    received_at: datetime
    true_category: str
    kind: str = "normal"   # normal | duplicate | suspected_duplicate | mismatch | negative | currency
    origin: int | None = None   # index of the original document for duplicates


def _tax(net: int, category: str, text: str) -> int:
    rate = 7 if "Hotel" in text else (0 if category == "TRAINING" and "seminar" in text.lower() else 19)
    return (net * rate + 50) // 100


def _random_received(rng: random.Random) -> datetime:
    day = START + timedelta(days=rng.randrange((END - START).days + 1))
    return datetime.combine(day, time(rng.randint(7, 17), rng.randrange(60)))


def generate_documents(rng: random.Random) -> list[Doc]:
    vendors = sorted(VENDORS)
    base_count = N_DOCUMENTS - INJECTED["exact_or_formatted_duplicates"] - INJECTED["suspected_duplicates"]
    counters: dict[str, int] = {}
    docs: list[Doc] = []
    for _ in range(base_count):
        vendor_id = rng.choice(vendors)
        name, vendor_cat, _ = VENDORS[vendor_id]
        received = _random_received(rng)
        if vendor_cat is None:
            text = rng.choice(AMBIGUOUS)
            category = rng.choice(("CONSULTING", "FACILITIES", "OFFICE_SUPPLIES"))
        else:
            # 85% the vendor's usual category, otherwise something else it also sells
            category = vendor_cat if rng.random() < 0.85 else rng.choice(sorted(CATEGORIES))
            text = rng.choice(TEMPLATES[category])
        text = text.format(n=rng.randint(2, 40), month=received.strftime("%B %Y"))
        low, high = NET_RANGE_EUR[category]
        net = rng.randint(low * 100, high * 100)
        tax = _tax(net, category, text)
        counters[vendor_id] = counters.get(vendor_id, 400) + rng.randint(1, 9)
        prefix = "".join(w[0] for w in name.split()[:2]).upper()
        number = f"{prefix}-2026-{counters[vendor_id]:05d}"
        invoice_date = (received.date() - timedelta(days=rng.randint(0, 6))).isoformat()
        docs.append(Doc(wf.InvoiceIn(number, vendor_id, text, net, tax, net + tax, invoice_date),
                        received, category))

    # --- anomalies on existing documents
    picks = rng.sample(range(len(docs)), INJECTED["header_amount_mismatch"]
                       + INJECTED["negative_amount"] + INJECTED["unsupported_currency"])
    for i, idx in enumerate(picks):
        d = docs[idx].doc
        if i < INJECTED["header_amount_mismatch"]:
            d.gross_cents += rng.choice((-1, 1)) * rng.randint(1, 5000)   # OCR misread / rounding
            docs[idx].kind = "mismatch"
        elif i < INJECTED["header_amount_mismatch"] + INJECTED["negative_amount"]:
            d.net_cents, d.tax_cents, d.gross_cents = -d.net_cents, -d.tax_cents, -d.gross_cents
            d.description = "Credit note: " + d.description
            docs[idx].kind = "negative"
        else:
            d.currency = "USD"
            docs[idx].kind = "currency"

    # --- duplicates: re-sent documents (sometimes with a re-formatted number)
    normal = [i for i, d in enumerate(docs) if d.kind == "normal"]
    for idx in rng.sample(normal, INJECTED["exact_or_formatted_duplicates"]):
        src = docs[idx]
        number = src.doc.invoice_number
        if rng.random() < 0.5:
            number = number.replace("-", " ").lower().replace("2026 0", "2026 ")
        dup = wf.InvoiceIn(number, src.doc.vendor_id, src.doc.description, src.doc.net_cents,
                           src.doc.tax_cents, src.doc.gross_cents, src.doc.invoice_date)
        docs.append(Doc(dup, src.received_at + timedelta(days=rng.randint(1, 20), hours=rng.randint(0, 5)),
                        src.true_category, "duplicate", idx))
    for idx in rng.sample(normal, INJECTED["suspected_duplicates"]):
        src = docs[idx]
        reissued = wf.InvoiceIn(src.doc.invoice_number + "-R", src.doc.vendor_id, src.doc.description,
                                src.doc.net_cents, src.doc.tax_cents, src.doc.gross_cents,
                                (date.fromisoformat(src.doc.invoice_date) + timedelta(days=rng.randint(0, 3))).isoformat())
        docs.append(Doc(reissued, src.received_at + timedelta(days=rng.randint(2, 12)),
                        src.true_category, "suspected_duplicate", idx))
    # keep generated documents within the window, then sort chronologically
    for d in docs:
        if d.received_at.date() > END:
            d.received_at = datetime.combine(END, time(17, 30))
    docs.sort(key=lambda d: (d.received_at, d.doc.vendor_id, d.doc.invoice_number))
    return docs


def _next_run(after: datetime, at: time) -> datetime:
    candidate = datetime.combine(after.date(), at)
    return candidate if candidate >= after else candidate + timedelta(days=1)


def _seed_master_data(conn: sqlite3.Connection) -> None:
    conn.executemany("INSERT INTO cost_centers VALUES (?,?,?,?,?)",
                     [(cid, *vals) for cid, vals in sorted(COST_CENTERS.items())])
    conn.executemany("INSERT INTO vendors VALUES (?,?,?)",
                     [(vid, name, field) for vid, (name, _, field) in sorted(VENDORS.items())])
    conn.execute("CREATE TABLE IF NOT EXISTS demo_ground_truth (invoice_id INTEGER PRIMARY KEY, "
                 "category TEXT NOT NULL, cost_center_id TEXT NOT NULL, injected_kind TEXT NOT NULL)")
    conn.commit()


def _log_llm_call(conn: sqlite3.Connection, when: datetime, invoice_id: int, reason: str) -> None:
    timed_out = reason == "fallback:llm_timeout"
    conn.execute(
        "INSERT INTO api_call_log (ts, method, endpoint, invoice_id, attempt, status_code, outcome, error) "
        "VALUES (?, 'POST', '/v1/classify (LLM)', ?, 1, ?, ?, ?)",
        (ts(when), invoice_id, None if timed_out else 200, "timeout" if timed_out else "ok",
         "LLM endpoint timed out, rules fallback used" if timed_out else None),
    )


def build(db_path: str | Path = DEFAULT_DB_PATH) -> dict:
    rng = random.Random(SEED)
    conn = connect(db_path)
    reset(conn)
    conn.execute("DROP TABLE IF EXISTS demo_ground_truth")
    _seed_master_data(conn)

    config = ClassifierConfig(llm_url=MOCK_URL, threshold=0.75)
    llm = InvoiceClassifier(config, transport=mock_transport())

    def _down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated LLM outage", request=request)

    llm_down = InvoiceClassifier(config, transport=httpx.MockTransport(_down))

    docs = generate_documents(rng)
    timeline: list[tuple[int, Doc]] = []
    for d in docs:                                            # 1. intake, chronological
        result = wf.receive_invoice(conn, d.doc, d.received_at)
        inv_id = result.invoice["id"]
        conn.execute("INSERT INTO demo_ground_truth VALUES (?,?,?,?)",
                     (inv_id, d.true_category, default_cost_center(d.true_category), d.kind))
        timeline.append((inv_id, d))
    conn.commit()

    for inv_id, d in timeline:
        inv = wf.get_invoice(conn, inv_id)
        if inv["status"] != "received":
            continue                                          # duplicate / on_hold stop here
        t = d.received_at + timedelta(minutes=rng.randint(2, 20))
        classifier = llm_down if d.received_at.date() in LLM_OUTAGE_DAYS else llm
        result = classifier.classify(inv["description"], VENDORS[inv["vendor_id"]][0])
        _log_llm_call(conn, t, inv_id, result.reason)
        inv = wf.classify_invoice(conn, inv_id, _Fixed(result))  # 2. AI step

        if inv["status"] == "needs_review":                   # 3. human review
            t = t + timedelta(hours=rng.randint(2, 30))
            if t > SNAPSHOT_AT:
                continue
            wf.review_classification(conn, inv_id, default_cost_center(d.true_category), d.true_category)

        t = t + timedelta(minutes=rng.randint(1, 30))
        if t > SNAPSHOT_AT:
            continue
        approval = wf.request_approval(conn, inv_id, t)       # 4. approval
        manager = approval["approver"]
        bucket = rng.random()
        delay = timedelta(hours=rng.randint(1, 24) if bucket < 0.45 else
                          rng.randint(25, 71) if bucket < 0.75 else rng.randint(73, 160))
        is_dup = d.kind == "suspected_duplicate"
        reject = rng.random() < (0.5 if is_dup else 0.10)
        comment = ("Duplicate of an already received invoice" if is_dup else rng.choice(REJECT_REASONS)) \
            if reject else None
        decision = "rejected" if reject else "approved"
        overdue_at = t + timedelta(days=wf.ESCALATION_DAYS)
        if delay < timedelta(days=wf.ESCALATION_DAYS):
            if t + delay <= SNAPSHOT_AT:
                wf.decide(conn, inv_id, manager, decision, comment, t + delay)
            continue
        escalate_at = _next_run(overdue_at, ESCALATION_RUN)
        if escalate_at > SNAPSHOT_AT:
            continue                                          # overdue, flow has not run yet
        wf.escalate(conn, approval["id"], escalate_at)
        lead_delay = timedelta(hours=rng.randint(4, 72))
        if escalate_at + lead_delay <= SNAPSHOT_AT:
            wf.decide(conn, inv_id, FINANCE_LEAD, decision, comment, escalate_at + lead_delay)

    # 5. nightly booking interface over HTTP with injected faults
    from app.main import create_app
    clock = {"now": SNAPSHOT_AT}
    api = create_app(db_path, classifier=llm, clock=lambda: clock["now"])
    stats_total = {"booked": 0, "recovered_after_timeout": 0, "parked": 0, "attempts": 0}
    with TestClient(api) as http:
        client = FaultInjectingClient(http, FaultPlan())
        day = START
        while datetime.combine(day, BOOKING_RUN) <= SNAPSHOT_AT:
            run_at = datetime.combine(day, BOOKING_RUN)
            clock["now"] = run_at
            stats = run_booking_sync(conn, client, run_at)
            stats_total["booked"] += len(stats.booked)
            stats_total["recovered_after_timeout"] += len(stats.recovered_after_timeout)
            stats_total["parked"] += len(stats.parked)
            stats_total["attempts"] += stats.attempts
            day += timedelta(days=1)
    llm.close()
    llm_down.close()
    conn.commit()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status ORDER BY status").fetchall())
    conn.close()
    return {"documents": len(docs), "status_counts": counts, "booking": stats_total}


class _Fixed:
    """Hands an already computed classification to ``workflow.classify_invoice``."""

    def __init__(self, result) -> None:
        self.result = result

    def classify(self, text: str, vendor_name: str | None = None):
        return self.result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    args = parser.parse_args()
    summary = build(args.db)
    print(f"generated {summary['documents']} documents into {args.db}")
    for status, n in summary["status_counts"].items():
        print(f"  {status:<17}{n:>4}")
    print(f"  booking interface: {summary['booking']}")


if __name__ == "__main__":
    main()
