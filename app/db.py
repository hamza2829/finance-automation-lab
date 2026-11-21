"""SQLite schema and connection helpers.

All money values are stored as integer cents to avoid floating point drift.
All timestamps are naive UTC ISO-8601 strings (``YYYY-MM-DDTHH:MM:SS``) so they
sort lexicographically and export cleanly to CSV for Power Query.
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = ROOT / "data" / "finance.db"

INVOICE_STATUSES = (
    "received",          # stored, not yet classified
    "needs_review",      # AI confidence below threshold -> human must classify
    "pending_approval",  # waiting for cost center manager
    "escalated",         # approval older than 3 days -> finance lead
    "approved",
    "rejected",
    "duplicate",         # same vendor + normalised invoice number already exists
    "on_hold",           # failed validation (negative amount, net + tax != gross)
)
OPEN_STATUSES = ("received", "needs_review", "pending_approval", "escalated")

SCHEMA = """
CREATE TABLE IF NOT EXISTS cost_centers (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    manager_name    TEXT NOT NULL,
    manager_email   TEXT NOT NULL,
    annual_budget_cents INTEGER NOT NULL CHECK (annual_budget_cents >= 0)
);

CREATE TABLE IF NOT EXISTS vendors (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    -- which invoice field the accounting interface books; 'gross' is correct,
    -- 'net' is a deliberately misconfigured mapping used by the error analysis
    booking_amount_field TEXT NOT NULL DEFAULT 'gross'
        CHECK (booking_amount_field IN ('gross', 'net'))
);

CREATE TABLE IF NOT EXISTS invoices (
    id                  INTEGER PRIMARY KEY,
    invoice_number      TEXT NOT NULL,
    invoice_number_norm TEXT NOT NULL,
    vendor_id           TEXT NOT NULL REFERENCES vendors(id),
    description         TEXT NOT NULL,
    document_type       TEXT NOT NULL DEFAULT 'invoice'
        CHECK (document_type IN ('invoice', 'credit_note')),
    net_cents           INTEGER NOT NULL,
    tax_cents           INTEGER NOT NULL,
    gross_cents         INTEGER NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'EUR',
    invoice_date        TEXT NOT NULL,
    received_at         TEXT NOT NULL,
    cost_center_id      TEXT REFERENCES cost_centers(id),
    booking_category    TEXT,
    classified_by       TEXT CHECK (classified_by IN ('llm', 'rules', 'human')),
    ai_confidence       REAL,
    status              TEXT NOT NULL,
    hold_reason         TEXT,
    duplicate_of        INTEGER REFERENCES invoices(id),
    closed_at           TEXT
);
CREATE INDEX IF NOT EXISTS ix_invoices_vendor_number ON invoices(vendor_id, invoice_number_norm);
CREATE INDEX IF NOT EXISTS ix_invoices_status ON invoices(status);

CREATE TABLE IF NOT EXISTS approvals (
    id            INTEGER PRIMARY KEY,
    invoice_id    INTEGER NOT NULL REFERENCES invoices(id),
    level         INTEGER NOT NULL CHECK (level IN (1, 2)),
    approver      TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'escalated')),
    comment       TEXT,
    requested_at  TEXT NOT NULL,
    decided_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_approvals_invoice ON approvals(invoice_id);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id            INTEGER PRIMARY KEY,
    invoice_id    INTEGER NOT NULL UNIQUE REFERENCES invoices(id),
    account       TEXT NOT NULL,
    cost_center_id TEXT NOT NULL,
    amount_cents  INTEGER NOT NULL,
    booked_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS api_call_log (
    id            INTEGER PRIMARY KEY,
    ts            TEXT NOT NULL,
    method        TEXT NOT NULL,
    endpoint      TEXT NOT NULL,
    invoice_id    INTEGER,
    attempt       INTEGER NOT NULL,
    status_code   INTEGER,           -- NULL when the call timed out
    outcome       TEXT NOT NULL CHECK (outcome IN ('ok', 'http_error', 'timeout')),
    error         TEXT
);
"""


def ts(value: datetime) -> str:
    """Format a datetime as the canonical storage string."""
    return value.replace(microsecond=0, tzinfo=None).isoformat(timespec="seconds")


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def db_path_from_env() -> Path:
    return Path(os.environ.get("FAL_DB_PATH", str(DEFAULT_DB_PATH)))


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open a connection with row access by name and foreign keys enabled."""
    target = str(path if path is not None else db_path_from_env())
    if target != ":memory:":
        Path(target).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def reset(conn: sqlite3.Connection) -> None:
    """Drop all tables (used by the seed script for a clean rebuild)."""
    for table in ("api_call_log", "ledger_entries", "approvals", "invoices", "vendors", "cost_centers"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.commit()
    init_schema(conn)
