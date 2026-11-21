"""Nightly booking interface: approved invoices -> accounting system.

This is the integration that the error analysis looks at. Every HTTP attempt
is written to ``api_call_log``. Retry policy:

* timeout or 5xx  -> retry (max ``max_attempts``, backoff 0s / 30s / 120s)
* 4xx             -> do not retry, invoice is *parked* for manual handling
* 200 on a retry  -> the earlier attempt reached the server before timing out;
                     the idempotent endpoint prevented a double booking

``FaultInjectingClient`` wraps any client with a ``post(url, json=...)``
method and injects deterministic faults (no randomness at runtime: the fault
for (invoice, attempt) is derived from a SHA-256 hash).
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx

from app.db import ts

BOOKINGS_ENDPOINT = "/accounting/bookings"
BACKOFF_SECONDS = (0, 30, 120)

DEFAULT_FAULT_RATES = {
    "http_503": 0.06,
    "http_500": 0.02,
    "timeout_before": 0.03,   # request never reached the server
    "timeout_after": 0.02,    # server committed, response lost
}


class PostClient(Protocol):  # pragma: no cover - typing only
    def post(self, url: str, json: Any = None) -> httpx.Response: ...


class FaultPlan:
    def __init__(self, rates: dict[str, float] | None = None, salt: str = "fal-2026") -> None:
        self.rates = dict(DEFAULT_FAULT_RATES if rates is None else rates)
        if sum(self.rates.values()) > 1:
            raise ValueError("fault rates must sum to <= 1")
        self.salt = salt

    def fault(self, invoice_id: int, attempt: int) -> str:
        digest = hashlib.sha256(f"{self.salt}:{invoice_id}:{attempt}".encode()).hexdigest()
        u = int(digest[:12], 16) / 16**12
        edge = 0.0
        for name, rate in self.rates.items():
            edge += rate
            if u < edge:
                return name
        return "ok"


class FaultInjectingClient:
    def __init__(self, inner: PostClient, plan: FaultPlan | None = None) -> None:
        self.inner = inner
        self.plan = plan or FaultPlan()
        self.attempts: dict[int, int] = defaultdict(int)

    def post(self, url: str, json: Any = None) -> httpx.Response:
        invoice_id = int(json["invoice_id"])
        self.attempts[invoice_id] += 1
        fault = self.plan.fault(invoice_id, self.attempts[invoice_id])
        if fault == "http_503":
            return httpx.Response(503, json={"error": "service_unavailable"})
        if fault == "http_500":
            return httpx.Response(500, json={"error": "internal_error"})
        if fault == "timeout_before":
            raise httpx.ReadTimeout("simulated timeout, request not processed")
        response = self.inner.post(url, json=json)
        if fault == "timeout_after":
            raise httpx.ReadTimeout("simulated timeout, response lost after server commit")
        return response


@dataclass
class SyncStats:
    booked: list[int] = field(default_factory=list)
    recovered_after_timeout: list[int] = field(default_factory=list)  # 200 on retry
    parked: dict[int, str] = field(default_factory=dict)              # 4xx, no retry
    exhausted: list[int] = field(default_factory=list)                # still failing after max attempts
    attempts: int = 0


def booking_amount(invoice: sqlite3.Row | dict, amount_field: str) -> int:
    """Interface mapping: which invoice field is sent as the booking amount."""
    return int(invoice["net_cents"] if amount_field == "net" else invoice["gross_cents"])


def pending_bookings(conn: sqlite3.Connection, up_to: datetime) -> list[sqlite3.Row]:
    """Approved, not booked, not parked by an earlier 4xx."""
    return conn.execute(
        """SELECT i.*, v.booking_amount_field FROM invoices i
           JOIN vendors v ON v.id = i.vendor_id
           WHERE i.status = 'approved' AND i.closed_at <= ?
             AND NOT EXISTS (SELECT 1 FROM ledger_entries l WHERE l.invoice_id = i.id)
             AND NOT EXISTS (SELECT 1 FROM api_call_log c WHERE c.invoice_id = i.id
                             AND c.endpoint = ? AND c.status_code BETWEEN 400 AND 499)
           ORDER BY i.id""",
        (ts(up_to), BOOKINGS_ENDPOINT),
    ).fetchall()


def _log(conn: sqlite3.Connection, when: datetime, invoice_id: int, attempt: int,
         status_code: int | None, outcome: str, error: str | None) -> None:
    conn.execute(
        "INSERT INTO api_call_log (ts, method, endpoint, invoice_id, attempt, status_code, outcome, error) "
        "VALUES (?, 'POST', ?, ?, ?, ?, ?, ?)",
        (ts(when), BOOKINGS_ENDPOINT, invoice_id, attempt, status_code, outcome, error),
    )
    conn.commit()  # release the write lock before the next HTTP call hits the same database


def run_booking_sync(conn: sqlite3.Connection, client: PostClient, now: datetime,
                     max_attempts: int = 3) -> SyncStats:
    stats = SyncStats()
    for inv in pending_bookings(conn, now):
        payload = {
            "invoice_id": inv["id"],
            "amount_cents": booking_amount(inv, inv["booking_amount_field"]),
            "posting_date": inv["invoice_date"],
        }
        done = False
        for attempt in range(1, max_attempts + 1):
            when = now + timedelta(seconds=BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)])
            stats.attempts += 1
            try:
                response = client.post(BOOKINGS_ENDPOINT, json=payload)
            except httpx.TimeoutException as exc:
                _log(conn, when, inv["id"], attempt, None, "timeout", str(exc))
                continue
            code = response.status_code
            if code in (200, 201):
                _log(conn, when, inv["id"], attempt, code, "ok", None)
                (stats.booked if code == 201 else stats.recovered_after_timeout).append(inv["id"])
                done = True
                break
            error = response.text[:300]
            _log(conn, when, inv["id"], attempt, code, "http_error", error)
            if 400 <= code < 500:
                stats.parked[inv["id"]] = error
                done = True
                break
        if not done:
            stats.exhausted.append(inv["id"])
    return stats
