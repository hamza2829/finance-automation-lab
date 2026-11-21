"""Monthly report numbers straight from the database (SQL) and the Excel file.

Same definitions as the Power Query scripts in ``powerquery/queries``:

open invoices   status received | needs_review | pending_approval | escalated,
                received before the first day of the next month
approval time   closed_at - received_at of invoices approved in the month
rejected items  invoices rejected in the month, with the approver's comment

Rounding: Python ``round`` (round half to even), same as M ``Number.Round``.

Usage: python -m powerquery.report [--month 2026-10] [--db ...] [--xlsx ...]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

from app.db import DEFAULT_DB_PATH, OPEN_STATUSES, ROOT, connect

DEFAULT_MONTH = "2026-10"
OUTPUT_DIR = Path(os.environ.get("FAL_OUTPUT_DIR", str(ROOT / "powerquery" / "output")))
THREE_DAYS = 3 * 24 * 3600


def month_bounds(report_month: str) -> tuple[datetime, datetime]:
    year, month = (int(p) for p in report_month.split("-"))
    if not 1 <= month <= 12:
        raise ValueError(f"invalid month {report_month!r}")
    start = datetime(year, month, 1)
    end = datetime(year + (month == 12), 1 if month == 12 else month + 1, 1)
    return start, end


def hours(seconds: float | None) -> float | None:
    return None if seconds is None else round(seconds / 3600, 2)


def summarise_seconds(seconds: list[int]) -> dict:
    ordered = sorted(seconds)
    n = len(ordered)
    if n == 0:
        median = None
    elif n % 2:
        median = ordered[n // 2]
    else:
        median = (ordered[n // 2 - 1] + ordered[n // 2]) / 2
    return {
        "approved_count": n,
        "avg_approval_hours": hours(sum(ordered) / n) if n else None,
        "median_approval_hours": hours(median),
        "max_approval_hours": hours(ordered[-1]) if n else None,
        "within_3_days_pct": round(sum(1 for s in ordered if s <= THREE_DAYS) / n * 100, 1) if n else None,
    }


def compute_from_db(conn: sqlite3.Connection, report_month: str = DEFAULT_MONTH) -> dict:
    start, end = (d.isoformat(timespec="seconds") for d in month_bounds(report_month))
    placeholders = ",".join("?" * len(OPEN_STATUSES))

    open_items = [dict(r) for r in conn.execute(
        f"""SELECT id, invoice_number, vendor_id, COALESCE(cost_center_id, '(unassigned)') AS cost_center_id,
                   status, gross_cents, received_at,
                   (CAST(strftime('%s', ?) AS INTEGER) - CAST(strftime('%s', received_at) AS INTEGER)) / 86400 AS age_days
            FROM invoices WHERE status IN ({placeholders}) AND received_at < ?
            ORDER BY received_at, id""",
        (end, *OPEN_STATUSES, end))]
    open_by_status = [dict(r) for r in conn.execute(
        f"""SELECT status, COUNT(*) AS count, SUM(gross_cents) AS gross_cents FROM invoices
            WHERE status IN ({placeholders}) AND received_at < ? GROUP BY status ORDER BY status""",
        (*OPEN_STATUSES, end))]

    approved = conn.execute(
        """SELECT i.id, i.cost_center_id,
                  CAST(strftime('%s', i.closed_at) AS INTEGER) - CAST(strftime('%s', i.received_at) AS INTEGER) AS secs,
                  EXISTS (SELECT 1 FROM approvals a WHERE a.invoice_id = i.id AND a.level = 2) AS escalated
           FROM invoices i WHERE i.status = 'approved' AND i.closed_at >= ? AND i.closed_at < ?
           ORDER BY i.id""", (start, end)).fetchall()
    by_cc = conn.execute(
        """SELECT cost_center_id, COUNT(*) AS n,
                  SUM(CAST(strftime('%s', closed_at) AS INTEGER) - CAST(strftime('%s', received_at) AS INTEGER)) AS total
           FROM invoices WHERE status = 'approved' AND closed_at >= ? AND closed_at < ?
           GROUP BY cost_center_id ORDER BY cost_center_id""", (start, end)).fetchall()

    rejected_items = [dict(r) for r in conn.execute(
        """SELECT i.id, i.invoice_number, i.vendor_id, i.cost_center_id, i.gross_cents, i.closed_at,
                  a.approver AS rejected_by, a.comment
           FROM invoices i LEFT JOIN approvals a ON a.invoice_id = i.id AND a.status = 'rejected'
           WHERE i.status = 'rejected' AND i.closed_at >= ? AND i.closed_at < ?
           ORDER BY i.id""", (start, end))]
    rejected_by_cc = [dict(r) for r in conn.execute(
        """SELECT cost_center_id, COUNT(*) AS count, SUM(gross_cents) AS gross_cents FROM invoices
           WHERE status = 'rejected' AND closed_at >= ? AND closed_at < ?
           GROUP BY cost_center_id ORDER BY cost_center_id""", (start, end))]

    summary = {
        "report_month": report_month,
        "open_count": len(open_items),
        "open_gross_cents": sum(r["gross_cents"] for r in open_items),
        **summarise_seconds([r["secs"] for r in approved]),
        "escalated_count": sum(1 for r in approved if r["escalated"]),
        "rejected_count": len(rejected_items),
        "rejected_gross_cents": sum(r["gross_cents"] for r in rejected_items),
    }
    return {
        "summary": summary,
        "open_by_status": open_by_status,
        "open_items": open_items,
        "approval_time_by_cost_center": [
            {"cost_center_id": r["cost_center_id"], "approved_count": r["n"], "avg_hours": hours(r["total"] / r["n"])}
            for r in by_cc],
        "rejected_by_cost_center": rejected_by_cc,
        "rejected_items": rejected_items,
    }


# --------------------------------------------------------------------------- Excel
SHEETS = {
    # sheet title -> (report key, columns)
    "Open invoices": ("open_items", ("id", "invoice_number", "vendor_id", "cost_center_id", "status",
                                     "gross_cents", "received_at", "age_days")),
    "Open by status": ("open_by_status", ("status", "count", "gross_cents")),
    "Approval time by CC": ("approval_time_by_cost_center", ("cost_center_id", "approved_count", "avg_hours")),
    "Rejected items": ("rejected_items", ("id", "invoice_number", "vendor_id", "cost_center_id", "gross_cents",
                                          "closed_at", "rejected_by", "comment")),
    "Rejected by CC": ("rejected_by_cost_center", ("cost_center_id", "count", "gross_cents")),
}
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
EUR_FORMAT = '#,##0.00 "EUR"'


def _autosize(ws) -> None:
    for idx, column in enumerate(ws.columns, start=1):
        width = max(len(str(c.value)) if c.value is not None else 0 for c in column)
        ws.column_dimensions[get_column_letter(idx)].width = min(60, max(10, width + 2))


def write_excel(report: dict, path: str | Path) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    wb.properties.creator = "finance-automation-lab"
    wb.properties.created = wb.properties.modified = datetime(2026, 11, 1)

    ws = wb.active
    ws.title = "Summary"
    ws.append(["metric", "value", "display"])
    for metric, value in report["summary"].items():
        display = f"{value / 100:,.2f} EUR" if metric.endswith("_cents") and value is not None else value
        ws.append([metric, value, display])
    ws.append([])
    ws.append(["note", "Generated demo data. Same definitions as powerquery/queries/*.pq.", None])
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
    _autosize(ws)

    for title, (key, cols) in SHEETS.items():
        sheet = wb.create_sheet(title)
        sheet.append(list(cols))
        for row in report[key]:
            sheet.append([row[c] for c in cols])
        if "gross_cents" in cols:
            sheet.cell(row=1, column=len(cols) + 1, value="gross_eur")
            gcol = cols.index("gross_cents") + 1
            for r in range(2, sheet.max_row + 1):
                c = sheet.cell(row=r, column=len(cols) + 1,
                               value=f"={get_column_letter(gcol)}{r}/100")
                c.number_format = EUR_FORMAT
        if sheet.max_row > 1:
            ref = f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}"
            table = Table(displayName="t" + "".join(ch for ch in title.title() if ch.isalnum()), ref=ref)
            table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium2", showRowStripes=True)
            sheet.add_table(table)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
        _autosize(sheet)
    wb.save(path)
    return path


def read_excel(path: str | Path) -> dict:
    """Read the workbook back (values only) for the CI comparison."""
    wb = load_workbook(path, data_only=False)
    summary = {}
    for metric, value, *_ in wb["Summary"].iter_rows(min_row=2, values_only=True):
        if metric and metric != "note":
            summary[metric] = value
    tables = {}
    for title, (key, cols) in SHEETS.items():
        rows = list(wb[title].iter_rows(min_row=2, values_only=True))
        tables[key] = [dict(zip(cols, r[:len(cols)])) for r in rows]
    return {"summary": summary, **tables}


def main() -> None:
    parser = argparse.ArgumentParser(description="Monthly invoice report from the database")
    parser.add_argument("--month", default=DEFAULT_MONTH)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--xlsx", default=None)
    args = parser.parse_args()
    report = compute_from_db(connect(args.db), args.month)
    out = write_excel(report, args.xlsx or OUTPUT_DIR / f"monthly_report_{args.month}.xlsx")
    for k, v in report["summary"].items():
        print(f"{k:<24}{v}")
    print(f"excel: {out}")


if __name__ == "__main__":
    main()
