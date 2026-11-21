"""Step-by-step Python port of the Power Query M scripts in ``powerquery/queries``.

Power Query itself cannot run in CI (no Excel / Power BI on Linux runners).
To still catch a divergence between the M logic and the database numbers,
this module re-implements every M query *on the same CSV export*, step by
step, with the same step names as comments. ``compare_report.py`` then checks
it against the SQL path in ``report.py``.

It is a port, not an M interpreter: if you change a .pq file, change the
matching function here (``tests/test_report.py`` checks that the step names
still line up).
"""
from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from powerquery.report import OPEN_STATUSES, hours, month_bounds, summarise_seconds


def _dt(value: str) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def load_invoices(folder: Path) -> list[dict]:
    """Invoices.pq: Source -> Promoted -> Typed -> CostCenterNull"""
    with (folder / "invoices.csv").open(encoding="utf-8", newline="") as fh:
        promoted = list(csv.DictReader(fh))
    typed = [{
        "id": int(r["id"]), "invoice_number": r["invoice_number"], "vendor_id": r["vendor_id"],
        "cost_center_id": r["cost_center_id"] or None, "booking_category": r["booking_category"] or None,
        "status": r["status"], "gross_cents": int(r["gross_cents"]),
        "received_at": _dt(r["received_at"]), "closed_at": _dt(r["closed_at"]),
    } for r in promoted]
    return typed


def load_approvals(folder: Path) -> list[dict]:
    """Approvals.pq: Source -> Promoted -> Typed"""
    with (folder / "approvals.csv").open(encoding="utf-8", newline="") as fh:
        promoted = list(csv.DictReader(fh))
    return [{
        "id": int(r["id"]), "invoice_id": int(r["invoice_id"]), "level": int(r["level"]),
        "approver": r["approver"], "status": r["status"], "comment": r["comment"] or None,
        "requested_at": _dt(r["requested_at"]), "decided_at": _dt(r["decided_at"]),
    } for r in promoted]


def open_invoices(invoices: list[dict], report_month: str) -> list[dict]:
    """OpenInvoices.pq"""
    _, month_end = month_bounds(report_month)                                  # MonthStart, MonthEnd
    filtered = [r for r in invoices if r["status"] in OPEN_STATUSES and r["received_at"] < month_end]
    unassigned = [{**r, "cost_center_id": r["cost_center_id"] or "(unassigned)"} for r in filtered]
    with_age = [{**r, "age_days": int((month_end - r["received_at"]).total_seconds() // 86400)}
                for r in unassigned]
    cols = ("id", "invoice_number", "vendor_id", "cost_center_id", "status", "gross_cents", "received_at", "age_days")
    selected = [{c: r[c] for c in cols} for r in with_age]
    return sorted(selected, key=lambda r: (r["received_at"], r["id"]))       # Sorted


def open_by_status(open_rows: list[dict]) -> list[dict]:
    """OpenInvoicesByStatus.pq"""
    grouped: dict[str, dict] = {}
    for r in open_rows:
        g = grouped.setdefault(r["status"], {"status": r["status"], "count": 0, "gross_cents": 0})
        g["count"] += 1
        g["gross_cents"] += r["gross_cents"]
    return [grouped[k] for k in sorted(grouped)]


def approval_time(invoices: list[dict], approvals: list[dict], report_month: str) -> list[dict]:
    """ApprovalTime.pq"""
    month_start, month_end = month_bounds(report_month)
    approved = [r for r in invoices if r["status"] == "approved" and r["closed_at"] is not None
                and month_start <= r["closed_at"] < month_end]
    with_seconds = [{**r, "approval_seconds": int((r["closed_at"] - r["received_at"]).total_seconds())}
                    for r in approved]
    escalated_ids = {a["invoice_id"] for a in approvals if a["level"] == 2}
    with_escalation = [{**r, "was_escalated": r["id"] in escalated_ids} for r in with_seconds]
    with_hours = [{**r, "approval_hours": hours(r["approval_seconds"])} for r in with_escalation]
    return sorted(with_hours, key=lambda r: r["id"])


def approval_time_by_cost_center(rows: list[dict]) -> list[dict]:
    """ApprovalTimeByCostCenter.pq"""
    groups: dict[str, list[int]] = {}
    for r in rows:
        groups.setdefault(r["cost_center_id"], []).append(r["approval_seconds"])
    return [{"cost_center_id": cc, "approved_count": len(s), "avg_hours": hours(sum(s) / len(s))}
            for cc, s in sorted(groups.items())]


def rejected_items(invoices: list[dict], approvals: list[dict], report_month: str) -> list[dict]:
    """RejectedItems.pq"""
    month_start, month_end = month_bounds(report_month)
    rejected = [r for r in invoices if r["status"] == "rejected" and r["closed_at"] is not None
                and month_start <= r["closed_at"] < month_end]
    rejected_approvals = [a for a in approvals if a["status"] == "rejected"]
    joined = []
    for r in rejected:                                                          # Joined + Expanded (left outer)
        matches = [a for a in rejected_approvals if a["invoice_id"] == r["id"]] or [None]
        for a in matches:
            joined.append({**r, "comment": a["comment"] if a else None, "rejected_by": a["approver"] if a else None})
    cols = ("id", "invoice_number", "vendor_id", "cost_center_id", "gross_cents", "closed_at", "rejected_by", "comment")
    return sorted(({c: r[c] for c in cols} for r in joined), key=lambda r: r["id"])


def rejected_by_cost_center(rows: list[dict]) -> list[dict]:
    """RejectedByCostCenter.pq"""
    grouped: dict[str, dict] = {}
    for r in rows:
        g = grouped.setdefault(r["cost_center_id"], {"cost_center_id": r["cost_center_id"], "count": 0, "gross_cents": 0})
        g["count"] += 1
        g["gross_cents"] += r["gross_cents"]
    return [grouped[k] for k in sorted(grouped)]


def compute_from_csv(folder: str | Path, report_month: str) -> dict:
    """Summary.pq plus all detail queries, in the canonical report shape."""
    folder = Path(folder)
    invoices, approvals = load_invoices(folder), load_approvals(folder)
    open_rows = open_invoices(invoices, report_month)
    at_rows = approval_time(invoices, approvals, report_month)
    rej_rows = rejected_items(invoices, approvals, report_month)
    summary = {
        "report_month": report_month,
        "open_count": len(open_rows),
        "open_gross_cents": sum(r["gross_cents"] for r in open_rows),
        **summarise_seconds([r["approval_seconds"] for r in at_rows]),
        "escalated_count": sum(1 for r in at_rows if r["was_escalated"]),
        "rejected_count": len(rej_rows),
        "rejected_gross_cents": sum(r["gross_cents"] for r in rej_rows),
    }

    def iso(rows: list[dict]) -> list[dict]:
        return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r.items()} for r in rows]

    return {
        "summary": summary,
        "open_by_status": open_by_status(open_rows),
        "open_items": iso(open_rows),
        "approval_time_by_cost_center": approval_time_by_cost_center(at_rows),
        "rejected_by_cost_center": rejected_by_cost_center(rej_rows),
        "rejected_items": iso(rej_rows),
    }
