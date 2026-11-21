import json
import re
from datetime import datetime

import pytest

from powerquery import compare_report, m_port, report
from powerquery.export_csv import COLUMNS, export

QUERIES = report.ROOT / "powerquery" / "queries"
PARAMETER_QUERIES = {"ReportMonth", "ExportFolder"}


def test_sql_and_m_port_agree(seeded_conn, tmp_path):
    export(seeded_conn, tmp_path)
    assert report.compute_from_db(seeded_conn, "2026-10") == m_port.compute_from_csv(tmp_path, "2026-10")


@pytest.mark.parametrize("month", ["2026-08", "2026-09"])
def test_sql_and_m_port_agree_for_other_months(seeded_conn, tmp_path, month):
    export(seeded_conn, tmp_path)
    assert report.compute_from_db(seeded_conn, month) == m_port.compute_from_csv(tmp_path, month)


def test_full_comparison_passes_against_committed_snapshot(seeded_db, tmp_path):
    snapshot = compare_report.EXPECTED_DIR / "report_2026-10.json"
    problems = compare_report.run("2026-10", seeded_db, tmp_path / "export", tmp_path / "r.xlsx", snapshot)
    assert problems == []


def test_comparison_reports_a_changed_number(seeded_db, tmp_path):
    snapshot = tmp_path / "snap.json"
    compare_report.run("2026-10", seeded_db, tmp_path / "export", tmp_path / "r.xlsx", snapshot, update_snapshot=True)
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    data["summary"]["open_count"] += 1
    snapshot.write_text(json.dumps(data), encoding="utf-8")
    problems = compare_report.run("2026-10", seeded_db, tmp_path / "export", tmp_path / "r.xlsx", snapshot)
    assert problems == [f"sql vs snapshot: $.summary.open_count: {data['summary']['open_count'] - 1} != {data['summary']['open_count']}"]


def test_excel_contains_report_sheets(seeded_conn, tmp_path):
    rep = report.compute_from_db(seeded_conn, "2026-10")
    path = report.write_excel(rep, tmp_path / "r.xlsx")
    back = report.read_excel(path)
    assert back == compare_report.excel_view(rep)
    assert back["summary"]["report_month"] == "2026-10"


def test_empty_month_has_zero_counts(seeded_conn):
    rep = report.compute_from_db(seeded_conn, "2026-01")
    s = rep["summary"]
    assert (s["approved_count"], s["rejected_count"], s["avg_approval_hours"], s["median_approval_hours"]) == (0, 0, None, None)


def test_month_bounds_roll_over_the_year():
    assert report.month_bounds("2026-12") == (datetime(2026, 12, 1), datetime(2027, 1, 1))
    with pytest.raises(ValueError):
        report.month_bounds("2026-13")


def test_median_of_even_count_is_mean_of_middle_values():
    s = report.summarise_seconds([3600, 7200, 10800, 14400])
    assert (s["median_approval_hours"], s["avg_approval_hours"], s["within_3_days_pct"]) == (2.5, 2.5, 100.0)


def test_every_m_query_has_a_python_port():
    source = (report.ROOT / "powerquery" / "m_port.py").read_text(encoding="utf-8")
    names = {p.stem for p in QUERIES.glob("*.pq")} - PARAMETER_QUERIES
    assert names and all(f"{n}.pq" in source for n in names)


def test_m_staging_queries_only_type_exported_columns():
    for table, query in (("invoices", "Invoices"), ("approvals", "Approvals")):
        text = (QUERIES / f"{query}.pq").read_text(encoding="utf-8")
        typed = set(re.findall(r'\{"(\w+)",\s*(?:Int64\.Type|type \w+)\}', text))
        assert typed == set(COLUMNS[table]), table


def test_open_items_have_a_cost_center_label(seeded_conn):
    rep = report.compute_from_db(seeded_conn, "2026-10")
    assert rep["open_items"] and all(r["cost_center_id"] for r in rep["open_items"])


def test_export_writes_both_csv_files(tmp_path, seeded_conn):
    written = export(seeded_conn, tmp_path)
    header = written["invoices"].read_text(encoding="utf-8").splitlines()[0]
    assert header == ",".join(COLUMNS["invoices"])
