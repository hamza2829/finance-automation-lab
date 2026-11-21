"""Structured error analysis of the invoice data flow.

Reads the database produced by ``python -m app.seed`` and writes
``analysis/error_report.md`` (for people) and ``analysis/error_report.json``
(for tests and CI). Everything is derived from the data: call log, invoices,
approvals, ledger. The demo ground truth table is only used for the AI
quality section.

Usage: python -m analysis.error_analysis [--db data/finance.db] [--out analysis]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import date
from pathlib import Path

from app.db import DEFAULT_DB_PATH, ROOT, connect

OUT_DIR = ROOT / "analysis"
SUSPECT_WINDOW_DAYS = 5


def eur(cents: int | None) -> str:
    return f"{(cents or 0) / 100:,.2f} EUR"


def _rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- sections
def failed_api_calls(conn: sqlite3.Connection) -> dict:
    by_endpoint = _rows(conn, """
        SELECT endpoint, COUNT(*) AS calls,
               SUM(outcome = 'ok') AS ok,
               SUM(outcome = 'timeout') AS timeouts,
               SUM(outcome = 'http_error' AND status_code >= 500) AS http_5xx,
               SUM(outcome = 'http_error' AND status_code BETWEEN 400 AND 499) AS http_4xx
        FROM api_call_log GROUP BY endpoint ORDER BY endpoint""")
    by_status = _rows(conn, """
        SELECT endpoint, COALESCE(CAST(status_code AS TEXT), 'timeout') AS status, COUNT(*) AS calls
        FROM api_call_log WHERE outcome != 'ok' GROUP BY endpoint, status ORDER BY endpoint, status""")
    booking = "/accounting/bookings"
    touched = conn.execute(
        "SELECT COUNT(DISTINCT invoice_id) FROM api_call_log WHERE endpoint = ? AND outcome != 'ok'",
        (booking,)).fetchone()[0]
    recovered = conn.execute(
        """SELECT COUNT(DISTINCT c.invoice_id) FROM api_call_log c
           JOIN ledger_entries l ON l.invoice_id = c.invoice_id
           WHERE c.endpoint = ? AND c.outcome != 'ok'""", (booking,)).fetchone()[0]
    idempotent_hits = _rows(conn, """
        SELECT invoice_id, attempt FROM api_call_log
        WHERE endpoint = ? AND outcome = 'ok' AND status_code = 200 ORDER BY invoice_id""", (booking,))
    parked = _rows(conn, """
        SELECT i.id AS invoice_id, i.invoice_date, i.closed_at, i.gross_cents,
               json_extract(c.error, '$.error') AS error_code
        FROM api_call_log c JOIN invoices i ON i.id = c.invoice_id
        WHERE c.endpoint = ? AND c.status_code BETWEEN 400 AND 499
        AND NOT EXISTS (SELECT 1 FROM ledger_entries l WHERE l.invoice_id = i.id)
        ORDER BY i.id""", (booking,))
    unbooked_other = _rows(conn, """
        SELECT i.id AS invoice_id, i.gross_cents FROM invoices i
        WHERE i.status = 'approved'
          AND NOT EXISTS (SELECT 1 FROM ledger_entries l WHERE l.invoice_id = i.id)
          AND NOT EXISTS (SELECT 1 FROM api_call_log c WHERE c.invoice_id = i.id
                          AND c.endpoint = ? AND c.status_code BETWEEN 400 AND 499)
        ORDER BY i.id""", (booking,))
    llm_timeouts = conn.execute(
        "SELECT COUNT(*) FROM api_call_log WHERE endpoint LIKE '/v1/classify%' AND outcome = 'timeout'").fetchone()[0]
    failed_total = conn.execute("SELECT COUNT(*) FROM api_call_log WHERE outcome != 'ok'").fetchone()[0]
    return {
        "total_calls": conn.execute("SELECT COUNT(*) FROM api_call_log").fetchone()[0],
        "failed_calls": failed_total,
        "by_endpoint": by_endpoint,
        "failed_by_status": by_status,
        "booking_invoices_with_failures": touched,
        "booking_recovered_by_retry": recovered,
        "booking_idempotent_retries": len(idempotent_hits),
        "booking_parked_4xx": parked,
        "booking_parked_gross_cents": sum(p["gross_cents"] for p in parked),
        "approved_not_booked_other": unbooked_other,
        "llm_timeouts": llm_timeouts,
    }


def amount_mismatches(conn: sqlite3.Connection) -> dict:
    header = _rows(conn, """
        SELECT id AS invoice_id, vendor_id, net_cents, tax_cents, gross_cents,
               gross_cents - (net_cents + tax_cents) AS difference_cents
        FROM invoices WHERE net_cents + tax_cents != gross_cents ORDER BY id""")
    ledger = _rows(conn, """
        SELECT i.id AS invoice_id, i.vendor_id, v.name AS vendor_name, v.booking_amount_field,
               i.gross_cents, l.amount_cents AS booked_cents, i.gross_cents - l.amount_cents AS difference_cents
        FROM ledger_entries l JOIN invoices i ON i.id = l.invoice_id JOIN vendors v ON v.id = i.vendor_id
        WHERE l.amount_cents != i.gross_cents ORDER BY i.id""")
    by_vendor: dict[str, dict] = {}
    for row in ledger:
        g = by_vendor.setdefault(row["vendor_id"], {"vendor_id": row["vendor_id"], "vendor_name": row["vendor_name"],
                                                    "booking_amount_field": row["booking_amount_field"],
                                                    "entries": 0, "difference_cents": 0})
        g["entries"] += 1
        g["difference_cents"] += row["difference_cents"]
    return {
        "header_mismatch": header,
        "ledger_mismatch": ledger,
        "ledger_mismatch_by_vendor": [by_vendor[k] for k in sorted(by_vendor)],
        "ledger_difference_cents": sum(r["difference_cents"] for r in ledger),
    }


def duplicates(conn: sqlite3.Connection) -> dict:
    caught = _rows(conn, """
        SELECT d.id AS invoice_id, d.duplicate_of, d.vendor_id, d.invoice_number, o.invoice_number AS original_number,
               d.gross_cents, d.invoice_number != o.invoice_number AS needed_normalisation
        FROM invoices d JOIN invoices o ON o.id = d.duplicate_of
        WHERE d.status = 'duplicate' ORDER BY d.id""")
    candidates = _rows(conn, """
        SELECT a.id AS first_id, b.id AS second_id, a.vendor_id, a.gross_cents,
               a.invoice_number AS first_number, b.invoice_number AS second_number,
               a.invoice_date AS first_date, b.invoice_date AS second_date,
               a.status AS first_status, b.status AS second_status
        FROM invoices a JOIN invoices b
          ON a.vendor_id = b.vendor_id AND a.gross_cents = b.gross_cents AND a.id < b.id
         AND a.invoice_number_norm != b.invoice_number_norm
        WHERE a.status NOT IN ('duplicate', 'on_hold') AND b.status NOT IN ('duplicate', 'on_hold')
        ORDER BY a.id, b.id""")
    suspected = [c for c in candidates
                 if abs((date.fromisoformat(c["second_date"]) - date.fromisoformat(c["first_date"])).days)
                 <= SUSPECT_WINDOW_DAYS]
    paid_twice = [s for s in suspected if s["first_status"] == "approved" and s["second_status"] == "approved"]
    return {
        "caught_at_intake": caught,
        "caught_needing_normalisation": sum(1 for c in caught if c["needed_normalisation"]),
        "suspected_missed": suspected,
        "suspected_both_approved": paid_twice,
        "suspected_both_approved_gross_cents": sum(s["gross_cents"] for s in paid_twice),
    }


def validation_holds(conn: sqlite3.Connection) -> dict:
    rows = _rows(conn, "SELECT id AS invoice_id, vendor_id, gross_cents, currency, hold_reason FROM invoices "
                       "WHERE status = 'on_hold' ORDER BY id")
    kinds: dict[str, int] = {}
    for r in rows:
        for part in (r["hold_reason"] or "").split("; "):
            key = part.split(":")[0]
            kinds[key] = kinds.get(key, 0) + 1
    return {"on_hold": rows, "by_reason": dict(sorted(kinds.items()))}


def ai_quality(conn: sqlite3.Connection) -> dict:
    has_truth = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'demo_ground_truth'").fetchone()
    by_source = _rows(conn, """
        SELECT COALESCE(classified_by, 'not classified') AS source, COUNT(*) AS invoices
        FROM invoices GROUP BY source ORDER BY source""")
    result = {"by_source": by_source, "auto_accepted": None}
    if has_truth:
        auto = conn.execute("""
            SELECT COUNT(*) AS n, SUM(i.booking_category = g.category) AS correct
            FROM invoices i JOIN demo_ground_truth g ON g.invoice_id = i.id
            WHERE i.classified_by IN ('llm', 'rules')""").fetchone()
        result["auto_accepted"] = {"invoices": auto["n"], "correct": auto["correct"] or 0,
                                   "accuracy_pct": round((auto["correct"] or 0) / auto["n"] * 100, 1) if auto["n"] else None}
    return result


def analyze(conn: sqlite3.Connection) -> dict:
    return {
        "documents": conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0],
        "failed_api_calls": failed_api_calls(conn),
        "amount_mismatches": amount_mismatches(conn),
        "duplicates": duplicates(conn),
        "validation_holds": validation_holds(conn),
        "ai": ai_quality(conn),
    }


# --------------------------------------------------------------------------- markdown
def _table(headers: list[str], rows: list[list]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


def _example(dup: dict) -> str:
    for c in dup["caught_at_intake"]:
        if c["needed_normalisation"]:
            return f", e.g. `{c['invoice_number']}` vs `{c['original_number']}`"
    return ""


def render_markdown(r: dict) -> str:
    api, amt, dup, hold, ai = (r["failed_api_calls"], r["amount_mismatches"], r["duplicates"],
                               r["validation_holds"], r["ai"])
    parked = api["booking_parked_4xx"]
    lines = [
        "# Error analysis: invoice data flow",
        "",
        "> Generated by `python -m analysis.error_analysis` from the demo database "
        f"({r['documents']} generated invoice documents, Aug-Oct 2026). Do not edit by hand; "
        "CI regenerates this file and fails if it differs. All data is synthetic.",
        "",
        "## Summary",
        "",
        *_table(["#", "Error class", "Count", "Amount affected", "Root cause", "Fix"], [
            [1, "Transient API failures (5xx, timeouts) on booking", f"{api['booking_invoices_with_failures']} invoices",
             "-", "Accounting API unavailable / slow (injected faults)",
             f"Retry with backoff (in place, {api['booking_recovered_by_retry']} recovered)"],
            [2, "Timeout after the server committed", f"{api['booking_idempotent_retries']} retries",
             "-", "Response lost after booking was written",
             "Idempotent booking endpoint (in place, no double booking)"],
            [3, "Booking rejected: posting period closed (HTTP 422)", f"{len(parked)} invoices",
             eur(api["booking_parked_gross_cents"]), "Posting date = invoice date, approval finished after the period close (day 5)",
             "Post into the open period with service date, alert AP instead of parking silently"],
            [4, "Ledger amount differs from invoice gross", f"{len(amt['ledger_mismatch'])} entries",
             eur(amt["ledger_difference_cents"]), "Interface mapping books net instead of gross for "
             + ", ".join(v["vendor_name"] for v in amt["ledger_mismatch_by_vendor"] if v["booking_amount_field"] == "net"),
             "Fix mapping, correction bookings, nightly ledger-vs-invoice reconciliation"],
            [5, "Header amounts inconsistent (net + tax != gross)", f"{len(amt['header_mismatch'])} documents",
             eur(sum(abs(h["difference_cents"]) for h in amt["header_mismatch"])) + " (abs. diff)",
             "OCR misread or vendor error", "Kept on hold by intake validation, request corrected invoice"],
            [6, "Duplicates caught at intake", f"{len(dup['caught_at_intake'])} documents",
             eur(sum(c["gross_cents"] for c in dup["caught_at_intake"])),
             f"Vendor re-sent the document ({dup['caught_needing_normalisation']} with re-formatted number)",
             "Normalised vendor + number check (in place)"],
            [7, "Suspected duplicates missed by intake", f"{len(dup['suspected_missed'])} pairs",
             eur(dup["suspected_both_approved_gross_cents"]) + " approved twice",
             "Re-issued with a new number, same amount", "Fuzzy check (vendor + amount + date window) as warning in the approval"],
            [8, "Other validation holds (negative amount, currency)",
             f"{hold['by_reason'].get('negative_amount', 0)} + {sum(v for k, v in hold['by_reason'].items() if k.startswith('unsupported_currency'))}",
             "-", "Credit note sent as invoice; USD document", "Detect credit notes; FX handling or manual route"],
            [9, "LLM endpoint timeouts", f"{api['llm_timeouts']} calls", "-", "Model endpoint outage (2 simulated days)",
             "Rules fallback (in place); more cases go to human review"],
        ]),
        "",
        "## 1. Failed API calls",
        "",
        f"{api['failed_calls']} of {api['total_calls']} logged calls failed.",
        "",
        *_table(["Endpoint", "Calls", "OK", "Timeouts", "5xx", "4xx"],
                [[e["endpoint"], e["calls"], e["ok"], e["timeouts"], e["http_5xx"], e["http_4xx"]] for e in api["by_endpoint"]]),
        "",
        "Failed calls by status:",
        "",
        *_table(["Endpoint", "Status", "Calls"], [[s["endpoint"], s["status"], s["calls"]] for s in api["failed_by_status"]]),
        "",
        f"- Invoices with at least one failed booking attempt: **{api['booking_invoices_with_failures']}**, "
        f"of which **{api['booking_recovered_by_retry']}** were booked after a retry.",
        f"- **{api['booking_idempotent_retries']}** retries got `200 already booked`: the first attempt timed out "
        "*after* the server had committed. Without the idempotent endpoint these would be double bookings.",
        f"- **{len(parked)}** invoices are parked after HTTP 422 `period_closed` "
        f"({eur(api['booking_parked_gross_cents'])}). They are approved but not in the ledger.",
        f"- Approved, not booked, without a 4xx: **{len(api['approved_not_booked_other'])}** "
        "(approved after the last nightly run or retries exhausted).",
        "",
        "Parked invoices:",
        "",
        *_table(["Invoice", "Invoice date", "Approved at", "Gross", "Error"],
                [[p["invoice_id"], p["invoice_date"], p["closed_at"], eur(p["gross_cents"]), p["error_code"]] for p in parked]),
        "",
        "**Fixes:** keep retry + idempotency; route 4xx to the AP mailbox immediately (same pattern as the "
        "`Intake_failed` handler in flow 1) instead of only parking; for closed periods post with the current "
        "posting date and keep the invoice date as service date; alert when retries are exhausted.",
        "",
        "## 2. Mismatching amounts",
        "",
        "### 2a. Ledger vs invoice",
        "",
        *_table(["Vendor", "Mapping", "Entries", "Missing in ledger"],
                [[f"{v['vendor_id']} {v['vendor_name']}", v["booking_amount_field"], v["entries"], eur(v["difference_cents"])]
                 for v in amt["ledger_mismatch_by_vendor"]]),
        "",
        "Every mismatching entry belongs to a vendor whose interface mapping sends `net` instead of `gross`: "
        "the difference equals the VAT amount. **Fix:** correct the mapping, post correction entries for the "
        "listed invoices, and add a nightly reconciliation (`ledger.amount_cents = invoice.gross_cents`).",
        "",
        "### 2b. Invoice header inconsistent",
        "",
        *_table(["Invoice", "Vendor", "Net", "Tax", "Gross", "Difference"],
                [[h["invoice_id"], h["vendor_id"], eur(h["net_cents"]), eur(h["tax_cents"]), eur(h["gross_cents"]),
                  eur(h["difference_cents"])] for h in amt["header_mismatch"]]),
        "",
        "These never reached approval: intake keeps them `on_hold`. **Fix:** request a corrected invoice; "
        "if OCR is the cause, compare against the OCR confidence and the PDF.",
        "",
        "## 3. Duplicate invoices",
        "",
        f"Caught at intake: **{len(dup['caught_at_intake'])}** "
        f"({dup['caught_needing_normalisation']} only because of number normalisation{_example(dup)}).",
        "",
        *_table(["Duplicate", "Original", "Vendor", "Number", "Original number"],
                [[c["invoice_id"], c["duplicate_of"], c["vendor_id"], c["invoice_number"], c["original_number"]]
                 for c in dup["caught_at_intake"]]),
        "",
        f"Suspected duplicates **not** caught (same vendor and gross, invoice dates within {SUSPECT_WINDOW_DAYS} days, "
        "different number):",
        "",
        *_table(["First", "Second", "Vendor", "Gross", "Numbers", "Statuses"],
                [[s["first_id"], s["second_id"], s["vendor_id"], eur(s["gross_cents"]),
                  f"{s['first_number']} / {s['second_number']}", f"{s['first_status']} / {s['second_status']}"]
                 for s in dup["suspected_missed"]]),
        "",
        f"{len(dup['suspected_both_approved'])} pair(s) were approved twice ({eur(dup['suspected_both_approved_gross_cents'])}). "
        "**Fix:** show a duplicate warning in the approval request when vendor + amount match within the window; "
        "block payment until AP confirms.",
        "",
        "## 4. Other validation holds",
        "",
        *_table(["Reason", "Documents"], [[k, v] for k, v in hold["by_reason"].items()]),
        "",
        "## 5. AI classification",
        "",
        *_table(["Classified by", "Invoices"], [[s["source"], s["invoices"]] for s in ai["by_source"]]),
        "",
    ]
    if ai["auto_accepted"]:
        a = ai["auto_accepted"]
        lines += [
            f"Auto-accepted proposals (confidence >= threshold, no human): {a['invoices']}, "
            f"{a['correct']} match the generated ground truth ({a['accuracy_pct']} %). The model is a deterministic "
            "mock, so this number says nothing about a real LLM; it only shows how the measurement works.",
            "",
        ]
    lines += [
        "## Where each error is detected",
        "",
        "```mermaid",
        "flowchart LR",
        "  A[Inbound invoice] --> B{Intake validation}",
        "  B -- duplicate / on_hold --> H[AP mailbox]",
        "  B --> C[AI classification]",
        "  C -- LLM timeout --> R[Rules fallback]",
        "  C -- confidence < threshold --> HR[Human review]",
        "  C --> D[Approval]",
        "  R --> D",
        "  HR --> D",
        "  D --> E[Nightly booking interface]",
        "  E -- 5xx / timeout --> E",
        "  E -- 422 period closed --> P[Parked]",
        "  E --> L[(Ledger)]",
        "  L -. reconciliation .-> X[Amount mismatch report]",
        "```",
        "",
    ]
    return "\n".join(lines)


def write(result: dict, out_dir: str | Path = OUT_DIR) -> tuple[Path, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md, js = out / "error_report.md", out / "error_report.json"
    md.write_text(render_markdown(result), encoding="utf-8", newline="\n")
    js.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    return md, js


def main() -> None:
    parser = argparse.ArgumentParser(description="Error analysis of the invoice data flow")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out", default=str(OUT_DIR))
    args = parser.parse_args()
    result = analyze(connect(args.db))
    md, js = write(result, args.out)
    print(f"wrote {md} and {js}")


if __name__ == "__main__":
    main()
