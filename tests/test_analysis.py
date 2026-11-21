from analysis import error_analysis as ea
from app import seed


def test_counts_match_injected_anomalies(seeded_conn):
    r = ea.analyze(seeded_conn)
    assert len(r["duplicates"]["caught_at_intake"]) == seed.INJECTED["exact_or_formatted_duplicates"]
    assert len(r["duplicates"]["suspected_missed"]) == seed.INJECTED["suspected_duplicates"]
    assert len(r["amount_mismatches"]["header_mismatch"]) == seed.INJECTED["header_amount_mismatch"]
    holds = r["validation_holds"]["by_reason"]
    assert holds["negative_amount"] == seed.INJECTED["negative_amount"]
    assert holds["unsupported_currency"] == seed.INJECTED["unsupported_currency"]


def test_ledger_mismatch_is_explained_by_net_mapping(seeded_conn):
    r = ea.analyze(seeded_conn)["amount_mismatches"]
    assert r["ledger_mismatch"], "the misconfigured vendor should produce mismatches"
    assert {v["booking_amount_field"] for v in r["ledger_mismatch_by_vendor"]} == {"net"}
    for row in r["ledger_mismatch"]:
        tax = seeded_conn.execute("SELECT tax_cents FROM invoices WHERE id = ?", (row["invoice_id"],)).fetchone()[0]
        assert row["difference_cents"] == tax


def test_parked_bookings_are_closed_periods(seeded_conn):
    api = ea.analyze(seeded_conn)["failed_api_calls"]
    assert api["booking_parked_4xx"]
    assert {p["error_code"] for p in api["booking_parked_4xx"]} == {"period_closed"}


def test_idempotent_retries_did_not_double_book(seeded_conn):
    api = ea.analyze(seeded_conn)["failed_api_calls"]
    assert api["booking_idempotent_retries"] > 0
    assert api["failed_calls"] == sum(e["calls"] - e["ok"] for e in api["by_endpoint"])


def test_llm_outage_is_visible(seeded_conn):
    api = ea.analyze(seeded_conn)["failed_api_calls"]
    assert api["llm_timeouts"] > 0


def test_markdown_has_all_sections(seeded_conn):
    md = ea.render_markdown(ea.analyze(seeded_conn))
    for heading in ("## Summary", "## 1. Failed API calls", "## 2. Mismatching amounts",
                    "## 3. Duplicate invoices", "## 4. Other validation holds", "## 5. AI classification", "```mermaid"):
        assert heading in md


def test_committed_report_is_up_to_date(seeded_conn, tmp_path):
    md, js = ea.write(ea.analyze(seeded_conn), tmp_path)
    committed = (ea.OUT_DIR / "error_report.md").read_text(encoding="utf-8")
    assert md.read_text(encoding="utf-8") == committed, "run: python -m analysis.error_analysis"
