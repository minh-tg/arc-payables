from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore
from arc_payables.currency import USDCOnlyConverter


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(_env_file=None, database_path=tmp_path / "arc_payables.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, suspicious_id = seed_demo(store)
    accounting = MockAccountingConnector(
        store,
        invoice_currency=settings.frappe_invoice_currency or "USD",
        settlement_to_invoice_rate=settings.settlement_to_invoice_rate,
    )
    payment = MockPaymentProvider(store)
    app = create_app(settings=settings, store=store, accounting=accounting, payment_provider=payment)
    client = TestClient(app)
    return {
        "settings": settings,
        "store": store,
        "accounting": accounting,
        "payment": payment,
        "workflow": app.state.workflow,
        "client": client,
        "legitimate_id": legitimate_id,
        "suspicious_id": suspicious_id,
    }
