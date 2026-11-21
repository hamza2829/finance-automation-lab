"""Export the tables the Power Query scripts read (invoices.csv, approvals.csv).

Usage: python -m powerquery.export_csv [--db data/finance.db] [--out data/export]
"""
from __future__ import annotations

import argparse
import csv
import os
import sqlite3
from pathlib import Path

from app.db import DEFAULT_DB_PATH, ROOT, connect

EXPORT_DIR = Path(os.environ.get("FAL_EXPORT_DIR", str(ROOT / "data" / "export")))
COLUMNS = {
    "invoices": ("id", "invoice_number", "vendor_id", "cost_center_id", "booking_category",
                 "status", "gross_cents", "received_at", "closed_at"),
    "approvals": ("id", "invoice_id", "level", "approver", "status", "comment",
                  "requested_at", "decided_at"),
}


def export(conn: sqlite3.Connection, out_dir: str | Path = EXPORT_DIR) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = {}
    for table, cols in COLUMNS.items():
        path = out / f"{table}.csv"
        rows = conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY id").fetchall()
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh, lineterminator="\n")
            writer.writerow(cols)
            for row in rows:
                writer.writerow(["" if v is None else v for v in row])
        written[table] = path
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Export CSV files for Power Query")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--out", default=str(EXPORT_DIR))
    args = parser.parse_args()
    conn = connect(args.db)
    for table, path in export(conn, args.out).items():
        print(f"{table}: {path}")


if __name__ == "__main__":
    main()
