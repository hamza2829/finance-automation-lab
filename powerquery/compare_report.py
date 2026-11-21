"""CI check: the report numbers agree across all four representations.

1. SQL on the database                     (powerquery/report.py)
2. Port of the M scripts on the CSV export (powerquery/m_port.py)
3. The generated Excel workbook            (read back with openpyxl)
4. The committed snapshot                  (powerquery/expected/report_<month>.json)

Exit code 0 when all agree, 1 otherwise (differences are printed).

Usage: python -m powerquery.compare_report [--month 2026-10] [--update-snapshot]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.db import DEFAULT_DB_PATH, ROOT, connect
from powerquery import m_port, report
from powerquery.export_csv import EXPORT_DIR, export

EXPECTED_DIR = ROOT / "powerquery" / "expected"


def diff(a: object, b: object, path: str = "$") -> list[str]:
    """Readable list of differences between two JSON-like values."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = []
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                out.append(f"{path}.{key}: only in {'right' if key not in a else 'left'}")
            else:
                out += diff(a[key], b[key], f"{path}.{key}")
        return out
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: length {len(a)} != {len(b)}"]
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [f"{path}: {a!r} != {b!r}"]


def excel_view(rep: dict) -> dict:
    """What the Excel workbook should contain for a report (same columns as the sheets)."""
    view = {"summary": rep["summary"]}
    for key, cols in ((k, c) for k, c in report.SHEETS.values()):
        view[key] = [{c: row[c] for c in cols} for row in rep[key]]
    return view


def run(month: str, db_path: str | Path, export_dir: str | Path, xlsx: str | Path,
        snapshot: str | Path, update_snapshot: bool = False) -> list[str]:
    conn = connect(db_path)
    sql = report.compute_from_db(conn, month)
    export(conn, export_dir)
    conn.close()
    m = m_port.compute_from_csv(export_dir, month)
    report.write_excel(sql, xlsx)
    excel = report.read_excel(xlsx)

    snapshot = Path(snapshot)
    if update_snapshot:
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.write_text(json.dumps(sql, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    problems = [f"sql vs m_port: {d}" for d in diff(sql, m)]
    problems += [f"sql vs excel: {d}" for d in diff(excel_view(sql), excel)]
    if snapshot.exists():
        expected = json.loads(snapshot.read_text(encoding="utf-8"))
        problems += [f"sql vs snapshot: {d}" for d in diff(sql, expected)]
    else:
        problems.append(f"snapshot {snapshot} missing (run with --update-snapshot)")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--month", default=report.DEFAULT_MONTH)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--update-snapshot", action="store_true")
    args = parser.parse_args()
    xlsx = report.OUTPUT_DIR / f"monthly_report_{args.month}.xlsx"
    snapshot = EXPECTED_DIR / f"report_{args.month}.json"
    problems = run(args.month, args.db, EXPORT_DIR, xlsx, snapshot, args.update_snapshot)
    if problems:
        print(f"REPORT MISMATCH ({len(problems)}):")
        for p in problems[:50]:
            print("  " + p)
        return 1
    print(f"report {args.month}: SQL == M port == Excel == snapshot ({xlsx.name})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
