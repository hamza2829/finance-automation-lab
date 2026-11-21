from datetime import datetime

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.sync import FaultInjectingClient, FaultPlan, booking_amount, run_booking_sync
from tests.conftest import NOW

RUN_AT = datetime(2026, 10, 1, 22, 0)


class ScriptedPlan(FaultPlan):
    """Faults per attempt number, independent of the invoice."""

    def __init__(self, script: list[str]) -> None:
        super().__init__(rates={})
        self.script = script

    def fault(self, invoice_id: int, attempt: int) -> str:
        return self.script[min(attempt, len(self.script)) - 1]


@pytest.fixture
def http(db_path):
    with TestClient(create_app(db_path, clock=lambda: RUN_AT)) as c:
        yield c


def test_fault_plan_is_deterministic():
    plan = FaultPlan()
    assert [plan.fault(i, 1) for i in range(200)] == [FaultPlan().fault(i, 1) for i in range(200)]
    assert {plan.fault(i, a) for i in range(500) for a in (1, 2)} >= {"ok", "http_503", "timeout_before"}


def test_fault_rates_must_not_exceed_one():
    with pytest.raises(ValueError):
        FaultPlan({"http_503": 0.7, "timeout_before": 0.5})


def test_booking_amount_follows_vendor_mapping():
    invoice = {"net_cents": 100, "gross_cents": 119}
    assert booking_amount(invoice, "gross") == 119
    assert booking_amount(invoice, "net") == 100


def test_retries_exhausted_after_three_5xx(conn, approved_invoice, http):
    stats = run_booking_sync(conn, FaultInjectingClient(http, ScriptedPlan(["http_503"])), RUN_AT)
    assert stats.exhausted == [approved_invoice["id"]] and stats.attempts == 3
    log = conn.execute("SELECT attempt, status_code, ts FROM api_call_log ORDER BY id").fetchall()
    assert [tuple(r) for r in log] == [(1, 503, "2026-10-01T22:00:00"), (2, 503, "2026-10-01T22:00:30"),
                                       (3, 503, "2026-10-01T22:02:00")]
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 0


def test_timeout_after_commit_is_recovered_without_double_booking(conn, approved_invoice, http):
    stats = run_booking_sync(conn, FaultInjectingClient(http, ScriptedPlan(["timeout_after", "ok"])), RUN_AT)
    assert stats.recovered_after_timeout == [approved_invoice["id"]]
    outcomes = [tuple(r) for r in conn.execute("SELECT outcome, status_code FROM api_call_log ORDER BY id")]
    assert outcomes == [("timeout", None), ("ok", 200)]
    assert conn.execute("SELECT COUNT(*) FROM ledger_entries").fetchone()[0] == 1


def test_timeout_before_server_then_success_books_once(conn, approved_invoice, http):
    stats = run_booking_sync(conn, FaultInjectingClient(http, ScriptedPlan(["timeout_before", "ok"])), RUN_AT)
    assert stats.booked == [approved_invoice["id"]]


def test_4xx_is_parked_and_not_retried(conn, approved_invoice, db_path):
    late = datetime(2026, 10, 6, 22, 0)        # September is closed since 5 October
    with TestClient(create_app(db_path, clock=lambda: late)) as http:
        client = FaultInjectingClient(http, ScriptedPlan(["ok"]))
        first = run_booking_sync(conn, client, late)
        second = run_booking_sync(conn, client, late)
    assert list(first.parked) == [approved_invoice["id"]] and "period_closed" in first.parked[approved_invoice["id"]]
    assert second.attempts == 0


def test_only_approved_invoices_before_run_time_are_picked(conn, approved_invoice, http):
    earlier = datetime(2026, 9, 30, 22, 0)    # invoice was approved on 1 October (NOW)
    assert NOW > earlier
    stats = run_booking_sync(conn, FaultInjectingClient(http, ScriptedPlan(["ok"])), earlier)
    assert stats.attempts == 0


def test_injected_http_error_is_a_real_response():
    class Inner:
        def post(self, url, json=None):  # pragma: no cover - never reached
            raise AssertionError

    response = FaultInjectingClient(Inner(), ScriptedPlan(["http_500"])).post("/x", json={"invoice_id": 1})
    assert isinstance(response, httpx.Response) and response.status_code == 500
