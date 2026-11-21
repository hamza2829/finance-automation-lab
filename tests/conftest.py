from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai.classifier import ClassifierConfig, InvoiceClassifier
from ai.mock_model import MOCK_URL, mock_transport
from app import workflow as wf
from app.db import connect, reset
from app.main import create_app
from app.seed import _seed_master_data, build

NOW = datetime(2026, 10, 1, 9, 0)
MANAGER_IT = "alex.brandt@example.com"


def make_doc(**overrides) -> wf.InvoiceIn:
    values = dict(invoice_number="KS-2026-00001", vendor_id="V-001",
                  description="Annual licence renewal office suite, 10 seats",
                  net_cents=100_000, tax_cents=19_000, gross_cents=119_000, invoice_date="2026-09-28")
    values.update(overrides)
    return wf.InvoiceIn(**values)


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "test.db"
    conn = connect(path)
    reset(conn)
    _seed_master_data(conn)
    conn.close()
    return path


@pytest.fixture
def conn(db_path: Path) -> sqlite3.Connection:
    c = connect(db_path)
    yield c
    c.close()


@pytest.fixture
def classifier() -> InvoiceClassifier:
    c = InvoiceClassifier(ClassifierConfig(llm_url=MOCK_URL, threshold=0.75), transport=mock_transport())
    yield c
    c.close()


@pytest.fixture
def clock() -> dict:
    return {"now": NOW}


@pytest.fixture
def client(db_path: Path, classifier: InvoiceClassifier, clock: dict) -> TestClient:
    api = create_app(db_path, classifier=classifier, clock=lambda: clock["now"])
    with TestClient(api) as c:
        yield c


@pytest.fixture
def approved_invoice(conn: sqlite3.Connection, classifier: InvoiceClassifier) -> dict:
    inv = wf.receive_invoice(conn, make_doc(), NOW).invoice
    wf.classify_invoice(conn, inv["id"], classifier)
    wf.request_approval(conn, inv["id"], NOW)
    return wf.decide(conn, inv["id"], MANAGER_IT, "approved", None, NOW)


@pytest.fixture(scope="session")
def seeded_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The full 300-invoice demo database, built once per test session."""
    path = tmp_path_factory.mktemp("seed") / "finance.db"
    build(path)
    return path


@pytest.fixture
def seeded_conn(seeded_db: Path) -> sqlite3.Connection:
    c = connect(seeded_db)
    yield c
    c.close()
