"""FastAPI mock of the accounting system and the approval API.

Run locally (after ``python -m app.seed``):

    uvicorn app.main:create_app --factory --port 8000

Nothing here talks to Microsoft or any other external service.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ai.classifier import ClassifierConfig, InvoiceClassifier
from app import workflow as wf
from app.db import connect, db_path_from_env, init_schema


# --------------------------------------------------------------------------- schemas
class InvoiceCreate(BaseModel):
    invoice_number: str = Field(min_length=1, max_length=64)
    vendor_id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=2000)
    net_cents: int
    tax_cents: int
    gross_cents: int
    invoice_date: date
    document_type: Literal["invoice", "credit_note"] = "invoice"
    currency: str = Field(default="EUR", pattern=r"^[A-Z]{3}$")


class ReviewIn(BaseModel):
    cost_center_id: str
    category: str


class DecisionIn(BaseModel):
    approver: str = Field(min_length=3)
    comment: str | None = None


class BookingIn(BaseModel):
    invoice_id: int
    amount_cents: int
    posting_date: date


# --------------------------------------------------------------------------- app factory
def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create_app(db_path: str | Path | None = None,
               classifier: InvoiceClassifier | None = None,
               clock: Callable[[], datetime] | None = None) -> FastAPI:
    api = FastAPI(
        title="Finance Automation Lab - mock accounting & approval API",
        version="0.1.0",
        description="Simulated system. Generated data only.",
    )
    api.state.db_path = str(db_path if db_path is not None else db_path_from_env())
    api.state.classifier = classifier
    api.state.clock = clock or _utcnow

    init_conn = connect(api.state.db_path)
    init_schema(init_conn)
    init_conn.close()

    def get_conn(request: Request) -> Iterator[sqlite3.Connection]:
        conn = connect(request.app.state.db_path)
        try:
            yield conn
        finally:
            conn.close()

    def now(request: Request) -> datetime:
        return request.app.state.clock()

    def get_classifier(request: Request) -> InvoiceClassifier:
        if request.app.state.classifier is None:
            request.app.state.classifier = InvoiceClassifier(ClassifierConfig.from_env())
        return request.app.state.classifier

    @api.exception_handler(wf.WorkflowError)
    def _workflow_error(_: Request, exc: wf.WorkflowError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code,
                            content={"error": exc.details.get("code", exc.code),
                                     "message": exc.message, "details": exc.details})

    # ---- health / master data
    @api.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @api.get("/cost-centers")
    def list_cost_centers(conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        return [dict(r) for r in conn.execute("SELECT * FROM cost_centers ORDER BY id")]

    @api.get("/cost-centers/{cc_id}")
    def get_cost_center(cc_id: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        row = conn.execute("SELECT * FROM cost_centers WHERE id = ?", (cc_id,)).fetchone()
        if row is None:
            raise wf.NotFound(f"cost center {cc_id} not found")
        return dict(row)

    # ---- invoices
    @api.get("/invoices")
    def list_invoices(status: str | None = None, cost_center_id: str | None = None,
                      limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
                      conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        where, params = [], []
        if status:
            where.append("status = ?")
            params.append(status)
        if cost_center_id:
            where.append("cost_center_id = ?")
            params.append(cost_center_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute(f"SELECT COUNT(*) FROM invoices{clause}", params).fetchone()[0]
        rows = conn.execute(f"SELECT * FROM invoices{clause} ORDER BY id LIMIT ? OFFSET ?",
                            [*params, limit, offset]).fetchall()
        return {"total": total, "items": [dict(r) for r in rows]}

    @api.get("/invoices/duplicates")
    def check_duplicate(vendor_id: str, invoice_number: str,
                        conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        norm = wf.normalize_invoice_number(invoice_number)
        original = wf.find_duplicate(conn, vendor_id, norm)
        return {"is_duplicate": original is not None, "duplicate_of": original, "normalized": norm}

    @api.get("/invoices/{invoice_id}")
    def get_invoice(invoice_id: int, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        inv = wf.get_invoice(conn, invoice_id)
        inv["approvals"] = [dict(r) for r in conn.execute(
            "SELECT * FROM approvals WHERE invoice_id = ? ORDER BY id", (invoice_id,))]
        return inv

    @api.post("/invoices", status_code=201)
    def create_invoice(body: InvoiceCreate, conn: sqlite3.Connection = Depends(get_conn),
                       current: datetime = Depends(now)) -> dict:
        doc = wf.InvoiceIn(**{**body.model_dump(), "invoice_date": body.invoice_date.isoformat()})
        result = wf.receive_invoice(conn, doc, current)
        return {"invoice": result.invoice, "issues": result.issues}

    @api.post("/invoices/{invoice_id}/classify")
    def classify(invoice_id: int, conn: sqlite3.Connection = Depends(get_conn),
                 classifier: InvoiceClassifier = Depends(get_classifier)) -> dict:
        inv = wf.classify_invoice(conn, invoice_id, classifier)
        return {"invoice": inv, "needs_human_review": inv["status"] == "needs_review",
                "threshold": classifier.config.threshold}

    @api.post("/invoices/{invoice_id}/review")
    def review(invoice_id: int, body: ReviewIn, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return wf.review_classification(conn, invoice_id, body.cost_center_id, body.category)

    @api.post("/invoices/{invoice_id}/approvals", status_code=201)
    def start_approval(invoice_id: int, conn: sqlite3.Connection = Depends(get_conn),
                       current: datetime = Depends(now)) -> dict:
        return wf.request_approval(conn, invoice_id, current)

    @api.post("/invoices/{invoice_id}/approve")
    def approve(invoice_id: int, body: DecisionIn, conn: sqlite3.Connection = Depends(get_conn),
                current: datetime = Depends(now)) -> dict:
        return wf.decide(conn, invoice_id, body.approver, "approved", body.comment, current)

    @api.post("/invoices/{invoice_id}/reject")
    def reject(invoice_id: int, body: DecisionIn, conn: sqlite3.Connection = Depends(get_conn),
               current: datetime = Depends(now)) -> dict:
        return wf.decide(conn, invoice_id, body.approver, "rejected", body.comment, current)

    # ---- approvals / escalation
    @api.get("/approvals/overdue")
    def overdue(days: int = Query(wf.ESCALATION_DAYS, ge=1), as_of: datetime | None = None,
                conn: sqlite3.Connection = Depends(get_conn), current: datetime = Depends(now)) -> list[dict]:
        return wf.overdue_approvals(conn, (as_of or current).replace(tzinfo=None), days)

    @api.post("/approvals/escalate-overdue")
    def escalate_overdue(days: int = Query(wf.ESCALATION_DAYS, ge=1), as_of: datetime | None = None,
                         conn: sqlite3.Connection = Depends(get_conn),
                         current: datetime = Depends(now)) -> list[dict]:
        return wf.escalate_overdue(conn, (as_of or current).replace(tzinfo=None), days)

    @api.post("/approvals/{approval_id}/escalate")
    def escalate(approval_id: int, conn: sqlite3.Connection = Depends(get_conn),
                 current: datetime = Depends(now)) -> dict:
        return wf.escalate(conn, approval_id, current)

    # ---- accounting system mock
    @api.post("/accounting/bookings")
    def post_booking(body: BookingIn, conn: sqlite3.Connection = Depends(get_conn),
                     current: datetime = Depends(now)) -> JSONResponse:
        entry, created = wf.book_invoice(conn, body.invoice_id, body.amount_cents,
                                         body.posting_date.isoformat(), current)
        return JSONResponse(status_code=201 if created else 200, content={"entry": entry, "created": created})

    @api.get("/accounting/ledger")
    def ledger(invoice_id: int | None = None, conn: sqlite3.Connection = Depends(get_conn)) -> list[dict]:
        if invoice_id is None:
            rows = conn.execute("SELECT * FROM ledger_entries ORDER BY id").fetchall()
        else:
            rows = conn.execute("SELECT * FROM ledger_entries WHERE invoice_id = ?", (invoice_id,)).fetchall()
        return [dict(r) for r in rows]

    return api
