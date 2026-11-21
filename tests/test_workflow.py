from datetime import date, datetime, timedelta

import pytest

from app import workflow as wf
from app.reference import FINANCE_LEAD
from tests.conftest import MANAGER_IT, NOW, make_doc


# --------------------------------------------------------------------------- amounts
@pytest.mark.parametrize("raw", ["KS-2026-00457", "ks 2026 457", "KS2026/0457", " ks-2026-457 "])
def test_invoice_number_normalisation_treats_formatting_variants_as_equal(raw):
    assert wf.normalize_invoice_number(raw) == "KS2026457"


def test_valid_amounts_have_no_issues():
    assert wf.validate_amounts(100, 19, 119) == []


def test_net_plus_tax_must_equal_gross():
    issues = wf.validate_amounts(100, 19, 120)
    assert issues == ["amount_mismatch: net+tax=119 gross=120"]


def test_negative_amount_on_invoice_is_rejected():
    assert "negative_amount" in wf.validate_amounts(-100, -19, -119)


def test_negative_credit_note_is_valid():
    assert wf.validate_amounts(-100, -19, -119, "credit_note") == []


def test_positive_credit_note_is_flagged():
    assert wf.validate_amounts(100, 19, 119, "credit_note") == ["credit_note_must_be_negative"]


def test_zero_amount_is_flagged():
    assert wf.validate_amounts(0, 0, 0) == ["zero_amount"]


# --------------------------------------------------------------------------- intake
def test_receive_valid_invoice(conn):
    result = wf.receive_invoice(conn, make_doc(), NOW)
    assert result.issues == []
    assert result.invoice["status"] == "received"
    assert result.invoice["invoice_number_norm"] == "KS20261"


def test_exact_duplicate_is_stored_as_duplicate(conn):
    first = wf.receive_invoice(conn, make_doc(), NOW).invoice
    second = wf.receive_invoice(conn, make_doc(), NOW + timedelta(days=2))
    assert second.invoice["status"] == "duplicate"
    assert second.invoice["duplicate_of"] == first["id"]
    assert f"duplicate_of:{first['id']}" in second.issues


def test_reformatted_number_is_detected_as_duplicate(conn):
    first = wf.receive_invoice(conn, make_doc(invoice_number="KS-2026-00457"), NOW).invoice
    second = wf.receive_invoice(conn, make_doc(invoice_number="ks 2026 457"), NOW).invoice
    assert second["status"] == "duplicate" and second["duplicate_of"] == first["id"]


def test_same_number_from_other_vendor_is_not_a_duplicate(conn):
    wf.receive_invoice(conn, make_doc(), NOW)
    other = wf.receive_invoice(conn, make_doc(vendor_id="V-002"), NOW).invoice
    assert other["status"] == "received"


def test_third_copy_points_to_the_original_not_to_the_copy(conn):
    original = wf.receive_invoice(conn, make_doc(), NOW).invoice
    wf.receive_invoice(conn, make_doc(), NOW)
    third = wf.receive_invoice(conn, make_doc(), NOW).invoice
    assert third["duplicate_of"] == original["id"]


def test_negative_invoice_is_kept_on_hold(conn):
    inv = wf.receive_invoice(conn, make_doc(net_cents=-100, tax_cents=-19, gross_cents=-119), NOW).invoice
    assert inv["status"] == "on_hold"
    assert "negative_amount" in inv["hold_reason"]


def test_unsupported_currency_is_kept_on_hold(conn):
    inv = wf.receive_invoice(conn, make_doc(currency="USD"), NOW).invoice
    assert inv["status"] == "on_hold" and "unsupported_currency" in inv["hold_reason"]


def test_unknown_vendor_is_unprocessable(conn):
    with pytest.raises(wf.Unprocessable):
        wf.receive_invoice(conn, make_doc(vendor_id="V-999"), NOW)


def test_blank_invoice_number_is_unprocessable(conn):
    with pytest.raises(wf.Unprocessable):
        wf.receive_invoice(conn, make_doc(invoice_number="   "), NOW)


# --------------------------------------------------------------------------- classification / review
def test_classification_sets_cost_center_and_category(conn, classifier):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    inv = wf.classify_invoice(conn, inv["id"], classifier)
    assert (inv["cost_center_id"], inv["booking_category"], inv["classified_by"]) == ("CC-100", "IT_SOFTWARE", "llm")
    assert inv["status"] == "received"


def test_low_confidence_goes_to_human_review(conn, classifier):
    inv = wf.receive_invoice(conn, make_doc(vendor_id="V-016", description="Services as agreed, see attachment"),
                             NOW).invoice
    inv = wf.classify_invoice(conn, inv["id"], classifier)
    assert inv["status"] == "needs_review"
    assert inv["ai_confidence"] < 0.75


def test_human_review_unknown_category_is_rejected(conn):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    with pytest.raises(wf.Unprocessable):
        wf.review_classification(conn, inv["id"], "CC-100", "PARTY")


def test_human_review_sets_classification(conn):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    inv = wf.review_classification(conn, inv["id"], "CC-500", "CONSULTING")
    assert (inv["classified_by"], inv["cost_center_id"], inv["status"]) == ("human", "CC-500", "received")


def test_cannot_classify_on_hold_invoice(conn, classifier):
    inv = wf.receive_invoice(conn, make_doc(gross_cents=1), NOW).invoice
    with pytest.raises(wf.Conflict):
        wf.classify_invoice(conn, inv["id"], classifier)


# --------------------------------------------------------------------------- approval
def test_approval_needs_classification_first(conn):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    with pytest.raises(wf.Conflict):
        wf.request_approval(conn, inv["id"], NOW)


def test_approval_is_assigned_to_cost_center_manager(conn, classifier):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    wf.classify_invoice(conn, inv["id"], classifier)
    appr = wf.request_approval(conn, inv["id"], NOW)
    assert (appr["approver"], appr["level"], appr["status"]) == (MANAGER_IT, 1, "pending")
    assert wf.get_invoice(conn, inv["id"])["status"] == "pending_approval"


@pytest.fixture
def pending(conn, classifier):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    wf.classify_invoice(conn, inv["id"], classifier)
    return inv["id"], wf.request_approval(conn, inv["id"], NOW)


def test_wrong_approver_is_forbidden(conn, pending):
    with pytest.raises(wf.Forbidden):
        wf.decide(conn, pending[0], "someone.else@example.com", "approved", None, NOW)


def test_approver_email_is_case_insensitive(conn, pending):
    inv = wf.decide(conn, pending[0], MANAGER_IT.upper(), "approved", None, NOW)
    assert inv["status"] == "approved" and inv["closed_at"] == "2026-10-01T09:00:00"


def test_rejection_requires_comment(conn, pending):
    with pytest.raises(wf.Unprocessable):
        wf.decide(conn, pending[0], MANAGER_IT, "rejected", "  ", NOW)


def test_invalid_decision_value(conn, pending):
    with pytest.raises(wf.Unprocessable):
        wf.decide(conn, pending[0], MANAGER_IT, "maybe", None, NOW)


def test_second_decision_is_a_conflict(conn, pending):
    wf.decide(conn, pending[0], MANAGER_IT, "rejected", "Not ordered", NOW)
    with pytest.raises(wf.Conflict):
        wf.decide(conn, pending[0], MANAGER_IT, "approved", None, NOW)


# --------------------------------------------------------------------------- escalation
def test_escalation_exactly_after_72_hours(conn, pending):
    inv_id, appr = pending
    as_of = NOW + timedelta(days=3)
    assert [a["id"] for a in wf.overdue_approvals(conn, as_of)] == [appr["id"]]
    lead = wf.escalate(conn, appr["id"], as_of)
    assert (lead["level"], lead["approver"], lead["status"]) == (2, FINANCE_LEAD, "pending")
    assert wf.get_approval(conn, appr["id"])["status"] == "escalated"
    assert wf.get_invoice(conn, inv_id)["status"] == "escalated"


def test_no_escalation_one_second_before_72_hours(conn, pending):
    as_of = NOW + timedelta(days=3) - timedelta(seconds=1)
    assert wf.overdue_approvals(conn, as_of) == []
    with pytest.raises(wf.Conflict):
        wf.escalate(conn, pending[1]["id"], as_of)


def test_escalation_cannot_run_twice(conn, pending):
    as_of = NOW + timedelta(days=4)
    wf.escalate(conn, pending[1]["id"], as_of)
    with pytest.raises(wf.Conflict):
        wf.escalate(conn, pending[1]["id"], as_of)


def test_level_two_approval_does_not_escalate_further(conn, pending):
    lead = wf.escalate(conn, pending[1]["id"], NOW + timedelta(days=3))
    with pytest.raises(wf.Conflict):
        wf.escalate(conn, lead["id"], NOW + timedelta(days=10))


def test_after_escalation_only_finance_lead_decides(conn, pending):
    inv_id, appr = pending
    wf.escalate(conn, appr["id"], NOW + timedelta(days=3))
    with pytest.raises(wf.Forbidden):
        wf.decide(conn, inv_id, MANAGER_IT, "approved", None, NOW + timedelta(days=3, hours=1))
    inv = wf.decide(conn, inv_id, FINANCE_LEAD, "approved", None, NOW + timedelta(days=3, hours=2))
    assert inv["status"] == "approved"


def test_decided_approvals_are_never_escalated(conn, pending):
    wf.decide(conn, pending[0], MANAGER_IT, "approved", None, NOW + timedelta(hours=1))
    assert wf.escalate_overdue(conn, NOW + timedelta(days=30)) == []


# --------------------------------------------------------------------------- accounting
def test_booking_is_idempotent(conn, approved_invoice):
    first, created = wf.book_invoice(conn, approved_invoice["id"], 119_000, "2026-09-28", NOW)
    again, created_again = wf.book_invoice(conn, approved_invoice["id"], 119_000, "2026-09-28", NOW)
    assert created and not created_again and first == again
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 1


def test_only_approved_invoices_are_booked(conn):
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    with pytest.raises(wf.Conflict):
        wf.book_invoice(conn, inv["id"], 119_000, "2026-09-28", NOW)


def test_closed_period_rejects_booking(conn, approved_invoice):
    with pytest.raises(wf.Unprocessable) as exc:
        wf.book_invoice(conn, approved_invoice["id"], 119_000, "2026-09-28", datetime(2026, 10, 5))
    assert exc.value.details["code"] == "period_closed"


@pytest.mark.parametrize("posting, now, closed", [
    (date(2026, 9, 30), datetime(2026, 10, 4, 23, 59, 59), False),
    (date(2026, 9, 30), datetime(2026, 10, 5, 0, 0, 0), True),
    (date(2026, 12, 31), datetime(2027, 1, 5), True),
    (date(2026, 12, 31), datetime(2027, 1, 4), False),
])
def test_period_close_boundaries(posting, now, closed):
    assert wf.is_period_closed(posting, now) is closed
