"""Business rules of the invoice approval process (no HTTP in here).

The FastAPI layer in ``app.main`` and the demo data generator in ``app.seed``
both go through these functions, so the generated data obeys the same rules
the API enforces. Every function takes an explicit ``now`` so behaviour is
deterministic and testable.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Protocol

from app.db import parse_ts, ts
from app.reference import CATEGORIES, FINANCE_LEAD, account_for

ESCALATION_DAYS = 3
PERIOD_CLOSE_DAY = 5  # month M is closed for postings from day 5 of month M+1, 00:00
SUPPORTED_CURRENCIES = ("EUR",)


# --------------------------------------------------------------------------- errors
class WorkflowError(Exception):
    status_code = 400
    code = "workflow_error"

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


class NotFound(WorkflowError):
    status_code = 404
    code = "not_found"


class Conflict(WorkflowError):
    status_code = 409
    code = "conflict"


class Forbidden(WorkflowError):
    status_code = 403
    code = "forbidden"


class Unprocessable(WorkflowError):
    status_code = 422
    code = "unprocessable"


# --------------------------------------------------------------------------- input
@dataclass
class InvoiceIn:
    invoice_number: str
    vendor_id: str
    description: str
    net_cents: int
    tax_cents: int
    gross_cents: int
    invoice_date: str
    document_type: str = "invoice"
    currency: str = "EUR"


class Classifier(Protocol):  # pragma: no cover - typing only
    def classify(self, text: str, vendor_name: str | None = None) -> Any: ...


@dataclass
class ReceiveResult:
    invoice: dict
    issues: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- helpers
_NON_ALNUM = re.compile(r"[^0-9A-Z]")


def normalize_invoice_number(number: str) -> str:
    """'inv-001023 ' and 'INV 1023' are the same document number.

    Upper-case, drop separators, and drop leading zeros of every digit group.
    """
    cleaned = _NON_ALNUM.sub(" ", number.upper()).split()
    parts = []
    for token in cleaned:
        # split letter/digit runs, strip leading zeros of digit runs
        for run in re.findall(r"[A-Z]+|[0-9]+", token):
            parts.append((run.lstrip("0") or "0") if run.isdigit() else run)
    return "".join(parts)


def validate_amounts(net: int, tax: int, gross: int, document_type: str = "invoice") -> list[str]:
    """Return a list of issue codes; an empty list means the amounts are fine."""
    issues: list[str] = []
    if net + tax != gross:
        issues.append(f"amount_mismatch: net+tax={net + tax} gross={gross}")
    if gross == 0:
        issues.append("zero_amount")
    if document_type == "invoice" and (net < 0 or tax < 0 or gross < 0):
        issues.append("negative_amount")
    if document_type == "credit_note" and gross > 0:
        issues.append("credit_note_must_be_negative")
    return issues


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def get_invoice(conn: sqlite3.Connection, invoice_id: int) -> dict:
    row = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
    if row is None:
        raise NotFound(f"invoice {invoice_id} not found", invoice_id=invoice_id)
    return dict(row)


def get_approval(conn: sqlite3.Connection, approval_id: int) -> dict:
    row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise NotFound(f"approval {approval_id} not found", approval_id=approval_id)
    return dict(row)


def find_duplicate(conn: sqlite3.Connection, vendor_id: str, number_norm: str) -> int | None:
    row = conn.execute(
        "SELECT id FROM invoices WHERE vendor_id = ? AND invoice_number_norm = ? "
        "AND status != 'duplicate' ORDER BY id LIMIT 1",
        (vendor_id, number_norm),
    ).fetchone()
    return row["id"] if row else None


def is_period_closed(posting_date: date, now: datetime) -> bool:
    """Month M closes at 00:00 on day PERIOD_CLOSE_DAY of month M+1."""
    year = posting_date.year + (1 if posting_date.month == 12 else 0)
    month = 1 if posting_date.month == 12 else posting_date.month + 1
    return now >= datetime(year, month, PERIOD_CLOSE_DAY)


# --------------------------------------------------------------------------- steps
def receive_invoice(conn: sqlite3.Connection, doc: InvoiceIn, now: datetime) -> ReceiveResult:
    """Store every inbound document. Invalid ones are kept 'on_hold', repeats as 'duplicate'.

    Nothing is silently dropped: finance must be able to show what arrived.
    """
    vendor = conn.execute("SELECT id FROM vendors WHERE id = ?", (doc.vendor_id,)).fetchone()
    if vendor is None:
        raise Unprocessable(f"unknown vendor {doc.vendor_id}", vendor_id=doc.vendor_id)
    if not doc.invoice_number.strip():
        raise Unprocessable("invoice_number must not be empty")

    issues = validate_amounts(doc.net_cents, doc.tax_cents, doc.gross_cents, doc.document_type)
    if doc.currency not in SUPPORTED_CURRENCIES:
        issues.append(f"unsupported_currency: {doc.currency}")
    norm = normalize_invoice_number(doc.invoice_number)
    duplicate_of = find_duplicate(conn, doc.vendor_id, norm)

    if duplicate_of is not None:
        status, hold_reason = "duplicate", f"duplicate_of:{duplicate_of}"
        issues.append(f"duplicate_of:{duplicate_of}")
    elif issues:
        status, hold_reason = "on_hold", "; ".join(issues)
    else:
        status, hold_reason = "received", None

    cur = conn.execute(
        """INSERT INTO invoices (invoice_number, invoice_number_norm, vendor_id, description,
               document_type, net_cents, tax_cents, gross_cents, currency, invoice_date,
               received_at, status, hold_reason, duplicate_of)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (doc.invoice_number, norm, doc.vendor_id, doc.description, doc.document_type,
         doc.net_cents, doc.tax_cents, doc.gross_cents, doc.currency, doc.invoice_date,
         ts(now), status, hold_reason, duplicate_of),
    )
    conn.commit()
    return ReceiveResult(get_invoice(conn, cur.lastrowid), issues)


def classify_invoice(conn: sqlite3.Connection, invoice_id: int, classifier: Classifier) -> dict:
    """Run the AI classifier. Below the confidence threshold the invoice goes to a human."""
    inv = get_invoice(conn, invoice_id)
    if inv["status"] not in ("received", "needs_review"):
        raise Conflict(f"invoice {invoice_id} cannot be classified in status {inv['status']}")
    vendor = conn.execute("SELECT name FROM vendors WHERE id = ?", (inv["vendor_id"],)).fetchone()
    result = classifier.classify(inv["description"], vendor_name=vendor["name"] if vendor else None)
    status = "needs_review" if result.needs_human_review else "received"
    conn.execute(
        "UPDATE invoices SET cost_center_id = ?, booking_category = ?, classified_by = ?, "
        "ai_confidence = ?, status = ? WHERE id = ?",
        (result.cost_center, result.category, result.source, result.confidence, status, invoice_id),
    )
    conn.commit()
    return get_invoice(conn, invoice_id)


def review_classification(conn: sqlite3.Connection, invoice_id: int, cost_center_id: str,
                          category: str) -> dict:
    """A human sets cost center and category (after a low-confidence AI proposal)."""
    inv = get_invoice(conn, invoice_id)
    if inv["status"] not in ("received", "needs_review"):
        raise Conflict(f"invoice {invoice_id} cannot be reviewed in status {inv['status']}")
    if category not in CATEGORIES:
        raise Unprocessable(f"unknown booking category {category}")
    if conn.execute("SELECT 1 FROM cost_centers WHERE id = ?", (cost_center_id,)).fetchone() is None:
        raise Unprocessable(f"unknown cost center {cost_center_id}")
    conn.execute(
        "UPDATE invoices SET cost_center_id = ?, booking_category = ?, classified_by = 'human', "
        "status = 'received' WHERE id = ?",
        (cost_center_id, category, invoice_id),
    )
    conn.commit()
    return get_invoice(conn, invoice_id)


def request_approval(conn: sqlite3.Connection, invoice_id: int, now: datetime) -> dict:
    """Create a level-1 approval for the manager of the invoice's cost center."""
    inv = get_invoice(conn, invoice_id)
    if inv["status"] != "received":
        raise Conflict(f"invoice {invoice_id} is {inv['status']}, approval needs status 'received'")
    if not inv["cost_center_id"] or not inv["booking_category"]:
        raise Conflict(f"invoice {invoice_id} has no cost center/category yet")
    cc = conn.execute("SELECT manager_email FROM cost_centers WHERE id = ?",
                      (inv["cost_center_id"],)).fetchone()
    cur = conn.execute(
        "INSERT INTO approvals (invoice_id, level, approver, status, requested_at) "
        "VALUES (?, 1, ?, 'pending', ?)",
        (invoice_id, cc["manager_email"], ts(now)),
    )
    conn.execute("UPDATE invoices SET status = 'pending_approval' WHERE id = ?", (invoice_id,))
    conn.commit()
    return get_approval(conn, cur.lastrowid)


def open_approval(conn: sqlite3.Connection, invoice_id: int) -> dict:
    row = conn.execute(
        "SELECT * FROM approvals WHERE invoice_id = ? AND status = 'pending' ORDER BY level DESC LIMIT 1",
        (invoice_id,),
    ).fetchone()
    if row is None:
        raise Conflict(f"invoice {invoice_id} has no pending approval")
    return dict(row)


def decide(conn: sqlite3.Connection, invoice_id: int, approver: str, decision: str,
           comment: str | None, now: datetime) -> dict:
    """Approve or reject. Only the currently assigned approver may decide."""
    if decision not in ("approved", "rejected"):
        raise Unprocessable(f"decision must be 'approved' or 'rejected', got {decision!r}")
    get_invoice(conn, invoice_id)
    appr = open_approval(conn, invoice_id)
    if approver.strip().lower() != appr["approver"].lower():
        raise Forbidden(f"{approver} is not the assigned approver", expected=appr["approver"])
    if decision == "rejected" and not (comment or "").strip():
        raise Unprocessable("a rejection needs a comment")
    conn.execute("UPDATE approvals SET status = ?, comment = ?, decided_at = ? WHERE id = ?",
                 (decision, comment, ts(now), appr["id"]))
    conn.execute("UPDATE invoices SET status = ?, closed_at = ? WHERE id = ?",
                 (decision, ts(now), invoice_id))
    conn.commit()
    return get_invoice(conn, invoice_id)


def overdue_approvals(conn: sqlite3.Connection, as_of: datetime,
                      days: int = ESCALATION_DAYS) -> list[dict]:
    """Pending level-1 approvals requested at least ``days`` x 24h before ``as_of``."""
    cutoff = ts(as_of - timedelta(days=days))
    rows = conn.execute(
        "SELECT * FROM approvals WHERE status = 'pending' AND level = 1 AND requested_at <= ? ORDER BY id",
        (cutoff,),
    ).fetchall()
    return [dict(r) for r in rows]


def escalate(conn: sqlite3.Connection, approval_id: int, now: datetime,
             days: int = ESCALATION_DAYS) -> dict:
    """Escalate an overdue level-1 approval to the finance lead (level 2)."""
    appr = get_approval(conn, approval_id)
    if appr["status"] != "pending":
        raise Conflict(f"approval {approval_id} is {appr['status']}, only pending ones escalate")
    if appr["level"] != 1:
        raise Conflict(f"approval {approval_id} is already level {appr['level']}")
    if now - parse_ts(appr["requested_at"]) < timedelta(days=days):
        raise Conflict(f"approval {approval_id} is not overdue yet")
    conn.execute(
        "UPDATE approvals SET status = 'escalated', decided_at = ?, comment = ? WHERE id = ?",
        (ts(now), f"no decision within {days} days", approval_id),
    )
    cur = conn.execute(
        "INSERT INTO approvals (invoice_id, level, approver, status, requested_at) "
        "VALUES (?, 2, ?, 'pending', ?)",
        (appr["invoice_id"], FINANCE_LEAD, ts(now)),
    )
    conn.execute("UPDATE invoices SET status = 'escalated' WHERE id = ?", (appr["invoice_id"],))
    conn.commit()
    return get_approval(conn, cur.lastrowid)


def escalate_overdue(conn: sqlite3.Connection, as_of: datetime,
                     days: int = ESCALATION_DAYS) -> list[dict]:
    return [escalate(conn, a["id"], as_of, days) for a in overdue_approvals(conn, as_of, days)]


# --------------------------------------------------------------------------- accounting mock
def book_invoice(conn: sqlite3.Connection, invoice_id: int, amount_cents: int,
                 posting_date: str, now: datetime) -> tuple[dict, bool]:
    """Accounting system: post an approved invoice. Idempotent per invoice.

    Returns ``(ledger_entry, created)``. A second call for the same invoice
    returns the existing entry with ``created=False`` - this is what makes
    retries after a timeout safe.
    """
    inv = get_invoice(conn, invoice_id)
    existing = conn.execute("SELECT * FROM ledger_entries WHERE invoice_id = ?", (invoice_id,)).fetchone()
    if existing is not None:
        return dict(existing), False
    if inv["status"] != "approved":
        raise Conflict(f"invoice {invoice_id} is {inv['status']}, only approved invoices are booked")
    if is_period_closed(date.fromisoformat(posting_date), now):
        raise Unprocessable(f"posting period of {posting_date} is closed", code="period_closed")
    cur = conn.execute(
        "INSERT INTO ledger_entries (invoice_id, account, cost_center_id, amount_cents, booked_at) "
        "VALUES (?,?,?,?,?)",
        (invoice_id, account_for(inv["booking_category"]), inv["cost_center_id"], amount_cents, ts(now)),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM ledger_entries WHERE id = ?", (cur.lastrowid,)).fetchone()
    return dict(row), True
