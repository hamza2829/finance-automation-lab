from datetime import timedelta

from tests.conftest import MANAGER_IT, NOW

PAYLOAD = {
    "invoice_number": "KS-2026-00777", "vendor_id": "V-001",
    "description": "Annual licence renewal office suite, 10 seats",
    "net_cents": 100_000, "tax_cents": 19_000, "gross_cents": 119_000, "invoice_date": "2026-09-28",
}


def create(client, **overrides) -> dict:
    response = client.post("/invoices", json={**PAYLOAD, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_cost_centers(client):
    centers = client.get("/cost-centers").json()
    assert [c["id"] for c in centers] == ["CC-100", "CC-200", "CC-300", "CC-400", "CC-500", "CC-600"]
    assert client.get("/cost-centers/CC-100").json()["manager_email"] == MANAGER_IT
    assert client.get("/cost-centers/CC-999").status_code == 404


def test_create_invoice(client):
    body = create(client)
    assert body["issues"] == [] and body["invoice"]["status"] == "received"


def test_duplicate_is_reported_but_stored(client):
    first = create(client)["invoice"]
    body = create(client, invoice_number="ks 2026 777")
    assert body["invoice"]["status"] == "duplicate"
    assert body["issues"] == [f"duplicate_of:{first['id']}"]
    check = client.get("/invoices/duplicates", params={"vendor_id": "V-001", "invoice_number": "KS2026777"}).json()
    assert check == {"is_duplicate": True, "duplicate_of": first["id"], "normalized": "KS2026777"}


def test_negative_amount_goes_on_hold(client):
    body = create(client, net_cents=-100_000, tax_cents=-19_000, gross_cents=-119_000)
    assert body["invoice"]["status"] == "on_hold" and "negative_amount" in body["issues"]


def test_schema_validation_errors(client):
    assert client.post("/invoices", json={**PAYLOAD, "invoice_date": "yesterday"}).status_code == 422
    missing = {k: v for k, v in PAYLOAD.items() if k != "gross_cents"}
    assert client.post("/invoices", json=missing).status_code == 422
    assert client.post("/invoices", json={**PAYLOAD, "currency": "euro"}).status_code == 422


def test_unknown_vendor_returns_error_body(client):
    response = client.post("/invoices", json={**PAYLOAD, "vendor_id": "V-404"})
    assert response.status_code == 422
    assert response.json()["error"] == "unprocessable"


def test_unknown_invoice_404(client):
    response = client.get("/invoices/9999")
    assert response.status_code == 404 and response.json()["error"] == "not_found"


def test_happy_path_to_ledger(client, clock):
    inv = create(client)["invoice"]
    classified = client.post(f"/invoices/{inv['id']}/classify").json()
    assert classified["needs_human_review"] is False and classified["threshold"] == 0.75
    approval = client.post(f"/invoices/{inv['id']}/approvals")
    assert approval.status_code == 201 and approval.json()["approver"] == MANAGER_IT
    assert client.get(f"/approvals/{approval.json()['id']}").json()["status"] == "pending"
    clock["now"] = NOW + timedelta(hours=5)
    approved = client.post(f"/invoices/{inv['id']}/approve", json={"approver": MANAGER_IT})
    assert approved.json()["status"] == "approved"
    booking = {"invoice_id": inv["id"], "amount_cents": 119_000, "posting_date": "2026-09-28"}
    first = client.post("/accounting/bookings", json=booking)
    second = client.post("/accounting/bookings", json=booking)
    assert (first.status_code, second.status_code) == (201, 200)
    assert second.json()["created"] is False
    assert len(client.get("/accounting/ledger", params={"invoice_id": inv["id"]}).json()) == 1
    detail = client.get(f"/invoices/{inv['id']}").json()
    assert [a["status"] for a in detail["approvals"]] == ["approved"]


def test_low_confidence_classification_flags_review(client):
    inv = create(client, vendor_id="V-016", description="Miscellaneous items October 2026")["invoice"]
    body = client.post(f"/invoices/{inv['id']}/classify").json()
    assert body["needs_human_review"] is True
    reviewed = client.post(f"/invoices/{inv['id']}/review", json={"cost_center_id": "CC-300", "category": "FACILITIES"})
    assert reviewed.json()["classified_by"] == "human"


def _pending(client) -> dict:
    inv = create(client)["invoice"]
    client.post(f"/invoices/{inv['id']}/classify")
    return client.post(f"/invoices/{inv['id']}/approvals").json()


def test_reject_needs_comment_and_right_approver(client):
    appr = _pending(client)
    url = f"/invoices/{appr['invoice_id']}/reject"
    assert client.post(url, json={"approver": MANAGER_IT}).status_code == 422
    assert client.post(url, json={"approver": "intruder@example.com", "comment": "no"}).status_code == 403
    assert client.post(url, json={"approver": MANAGER_IT, "comment": "Not ordered"}).json()["status"] == "rejected"


def test_overdue_and_escalation_endpoints(client):
    appr = _pending(client)
    early = client.get("/approvals/overdue", params={"as_of": (NOW + timedelta(days=2)).isoformat()}).json()
    assert early == []
    as_of = (NOW + timedelta(days=3, hours=1)).isoformat()
    assert [a["id"] for a in client.get("/approvals/overdue", params={"as_of": as_of}).json()] == [appr["id"]]
    escalated = client.post("/approvals/escalate-overdue", params={"as_of": as_of}).json()
    assert len(escalated) == 1 and escalated[0]["level"] == 2
    assert client.post(f"/approvals/{appr['id']}/escalate").status_code == 409


def test_single_escalation_endpoint_checks_age(client):
    appr = _pending(client)
    assert client.post(f"/approvals/{appr['id']}/escalate").status_code == 409


def test_booking_unapproved_invoice_conflicts(client):
    inv = create(client)["invoice"]
    response = client.post("/accounting/bookings",
                           json={"invoice_id": inv["id"], "amount_cents": 1, "posting_date": "2026-09-28"})
    assert response.status_code == 409


def test_list_invoices_filter_and_paging(client):
    create(client)
    create(client, invoice_number="KS-2026-00778")
    create(client, invoice_number="KS-2026-00779", gross_cents=1)
    received = client.get("/invoices", params={"status": "received"}).json()
    assert received["total"] == 2
    page = client.get("/invoices", params={"limit": 1, "offset": 2}).json()
    assert page["total"] == 3 and len(page["items"]) == 1 and page["items"][0]["status"] == "on_hold"
    assert client.get("/invoices", params={"limit": 0}).status_code == 422
