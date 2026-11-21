import hashlib
import random

from app import seed
from app.db import INVOICE_STATUSES


def test_seed_generates_300_documents(seeded_conn):
    assert seeded_conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 300


def test_document_generation_is_deterministic():
    def digest() -> str:
        docs = seed.generate_documents(random.Random(seed.SEED))
        text = "\n".join(f"{d.doc}|{d.received_at}|{d.kind}" for d in docs)
        return hashlib.sha256(text.encode()).hexdigest()

    assert digest() == digest()


def test_statuses_are_valid_and_anomalies_are_present(seeded_conn):
    counts = dict(seeded_conn.execute("SELECT status, COUNT(*) FROM invoices GROUP BY status").fetchall())
    assert set(counts) <= set(INVOICE_STATUSES)
    assert counts["duplicate"] == seed.INJECTED["exact_or_formatted_duplicates"]
    held = seed.INJECTED["header_amount_mismatch"] + seed.INJECTED["negative_amount"] + seed.INJECTED["unsupported_currency"]
    assert counts["on_hold"] == held


def test_every_closed_invoice_has_a_matching_decision(seeded_conn):
    bad = seeded_conn.execute("""
        SELECT i.id FROM invoices i WHERE i.status IN ('approved', 'rejected') AND NOT EXISTS (
            SELECT 1 FROM approvals a WHERE a.invoice_id = i.id AND a.status = i.status AND a.decided_at = i.closed_at)
    """).fetchall()
    assert bad == []


def test_escalations_respect_three_days(seeded_conn):
    rows = seeded_conn.execute("""
        SELECT (julianday(decided_at) - julianday(requested_at)) AS days FROM approvals WHERE status = 'escalated'
    """).fetchall()
    assert rows and min(r[0] for r in rows) >= 3


def test_ledger_only_contains_approved_invoices_once(seeded_conn):
    rows = seeded_conn.execute("""
        SELECT i.status, COUNT(*) FROM ledger_entries l JOIN invoices i ON i.id = l.invoice_id GROUP BY i.status
    """).fetchall()
    assert [r[0] for r in rows] == ["approved"]
    assert seeded_conn.execute("SELECT COUNT(*) - COUNT(DISTINCT invoice_id) FROM ledger_entries").fetchone()[0] == 0


def test_no_approval_without_classification(seeded_conn):
    assert seeded_conn.execute("""
        SELECT COUNT(*) FROM invoices i JOIN approvals a ON a.invoice_id = i.id
        WHERE i.cost_center_id IS NULL OR i.booking_category IS NULL""").fetchone()[0] == 0
