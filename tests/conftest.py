from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tameion.api import create_app
from tameion.mock_adapters import MockAccountingConnector, MockPaymentProvider
from tameion.policy import DeterministicPolicy
from tameion.seed import seed_demo
from tameion.settings import Settings
from tameion.store import SQLiteEvidenceStore
from tameion.currency import USDCOnlyConverter


@pytest.fixture
def runtime(tmp_path: Path):
    settings = Settings(_env_file=None, database_path=tmp_path / "tameion.sqlite3")
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
