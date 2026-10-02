"""A settlement whose network fee the provider could not name at the time.

This is how money that moved ends up unbooked for good. The provider cannot report a fee while the
transfer is still being indexed, the writeback has nothing to book, and the payment sits in the
ledger's debt. Retrying asks the same unanswerable question, so nothing ever resolves it: a live
invoice settled on chain and stayed unrecorded exactly this way.

The fee has to be re-read before it is declared unknowable, and the deferral has to be retryable
without re-reading the provider on every single pass.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc_payables import worker
from arc_payables.api import create_app
from arc_payables.domain import WorkflowState, utcnow
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

#: Every field ERPNext needs before a writeback is attempted at all. Without all of them the service
#: refuses the writeback as a mapping problem, which is a different disable from a missing fee.
FRAPPE_READY = {
    "frappe_url": "https://erp.example.test",
    "frappe_api_key": "test-key",
    "frappe_api_secret": "test-secret",
    "frappe_company": "Arc Demo Inc",
    "frappe_paid_from_account": "USDC Wallet - AD",
    "frappe_paid_to_account": "Accounts Payable - AD",
    "frappe_mode_of_payment": "Arc Testnet",
    "frappe_settlement_currency": "USDC",
    "frappe_company_currency": "USD",
    "frappe_invoice_currency": "USD",
    "frappe_source_exchange_rate": Decimal("1"),
    "frappe_target_exchange_rate": Decimal("1"),
    "frappe_fee_account": "Network Fees - AD",
    "frappe_fee_currency": "USD",
    "frappe_cost_center": "Main - AD",
}


def _step(report, name):
    return next(step for step in report.steps if step.name == name)


def _runtime(tmp_path: Path, *, deferred_fee: bool = True, accounting_ready: bool = True):
    """A Frappe-configured workflow over a mock connector, so the fee path is real and offline.

    Only `accounting_provider` decides that the network fee has to be booked as its own expense
    entry, and that is the path where an unreadable fee disables the writeback.
    """
    values = dict(FRAPPE_READY)
    if not accounting_ready:
        values["frappe_company"] = None
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "fee.sqlite3",
        accounting_provider="frappe",
        **values,
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _ = seed_demo(store)
    accounting = MockAccountingConnector(store, invoice_currency="USD", settlement_to_invoice_rate=Decimal("1"))
    payment = MockPaymentProvider(store, fee_units=10_000, deferred_fee=deferred_fee)
    app = create_app(settings=settings, store=store, accounting=accounting, payment_provider=payment)
    return {
        "settings": settings,
        "store": store,
        "accounting": accounting,
        "payment": payment,
        "workflow": app.state.workflow,
        "client": TestClient(app),
        "invoice_id": legitimate_id,
    }


def _settle(runtime) -> None:
    runtime["workflow"].evaluate(runtime["invoice_id"])
    runtime["workflow"].submit_payment(runtime["invoice_id"])


def test_a_fee_the_provider_could_not_name_is_read_again_and_the_payment_is_booked(tmp_path):
    """The whole point: a payment disabled for a missing fee can still reach the ledger."""
    runtime = _runtime(tmp_path)
    _settle(runtime)

    payment = runtime["store"].get_payment(runtime["invoice_id"])
    assert payment["fee_units"] is None, "the provider reported no fee, so none could be recorded"
    assert payment["erp_status"] == "DISABLED"
    assert payment["erp_error_code"] == "NETWORK_FEE_UNAVAILABLE"
    assert runtime["store"].get_state(runtime["invoice_id"]) == WorkflowState.ERP_PENDING.value

    # The transaction is indexed, so the provider can answer now. Before this fix nothing ever asked.
    runtime["payment"].deferred_fee = False
    report = worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))

    assert _step(report, "writeback").acted == 1
    payment = runtime["store"].get_payment(runtime["invoice_id"])
    assert payment["fee_units"] == 10_000
    assert payment["erp_fee_status"] == "RECORDED"
    assert payment["erp_attempts"] == 0
    assert payment["erp_next_attempt_at"] is None
    assert runtime["store"].get_state(runtime["invoice_id"]) == WorkflowState.ERP_RECORDED.value


def test_the_reread_records_what_it_learned_and_moves_nothing(tmp_path):
    """A repaired fee is an audit event, and it may not change the payment it belongs to."""
    runtime = _runtime(tmp_path)
    _settle(runtime)
    before = runtime["store"].get_payment(runtime["invoice_id"])

    runtime["payment"].deferred_fee = False
    worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))

    after = runtime["store"].get_payment(runtime["invoice_id"])
    for field in ("payment_id", "transaction_hash", "provider_transaction_id", "confirmation_status"):
        assert after[field] == before[field], f"a fee re-read must not touch {field}"
    events = [event["type"] for event in runtime["store"].events(runtime["invoice_id"])]
    assert "SETTLEMENT_FEE_REREAD" in events


def test_a_fee_that_stays_unreadable_is_deferred_not_abandoned(tmp_path):
    """It has to remain retryable, and it must not re-read the provider on every pass."""
    runtime = _runtime(tmp_path)
    _settle(runtime)

    first = runtime["store"].get_payment(runtime["invoice_id"])
    assert first["erp_error_code"] == "NETWORK_FEE_UNAVAILABLE"
    attempts = first["erp_attempts"]
    assert attempts >= 1, "a deferral has to carry a backoff or it is re-asked every pass"
    assert first["erp_next_attempt_at"] is not None
    assert datetime.fromisoformat(first["erp_next_attempt_at"]) > utcnow()

    # The very next pass waits rather than asking again.
    waiting = worker.run_pass(runtime["workflow"])
    writeback = _step(waiting, "writeback")
    assert writeback.acted == 0
    assert writeback.detail["waiting"] == 1

    # Once the backoff passes, it asks again and still does not invent a fee.
    report = worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))
    assert _step(report, "writeback").acted == 1
    payment = runtime["store"].get_payment(runtime["invoice_id"])
    assert payment["fee_units"] is None
    assert payment["erp_error_code"] == "NETWORK_FEE_UNAVAILABLE"
    assert payment["erp_attempts"] > attempts, "the backoff has to grow, not reset"
    assert runtime["store"].get_state(runtime["invoice_id"]) == WorkflowState.ERP_PENDING.value


def test_a_writeback_disabled_by_configuration_is_never_retried(tmp_path):
    """A mapping problem cannot be retried into working, and every attempt writes an event."""
    runtime = _runtime(tmp_path, accounting_ready=False)
    _settle(runtime)

    payment = runtime["store"].get_payment(runtime["invoice_id"])
    assert payment["erp_error_code"] == "ACCOUNTING_MAPPING_INCOMPLETE"

    report = worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(days=1))
    writeback = _step(report, "writeback")
    assert writeback.acted == 0
    assert writeback.skipped == 1
    assert runtime["store"].get_payment(runtime["invoice_id"])["erp_error_code"] == "ACCOUNTING_MAPPING_INCOMPLETE"


def test_a_provider_that_cannot_be_reached_leaves_the_record_alone(tmp_path):
    """The re-read is best effort. An unreachable provider must not rewrite anything."""

    def unreachable(payment):
        raise RuntimeError("provider is down")

    runtime = _runtime(tmp_path)
    _settle(runtime)
    before = runtime["store"].get_payment(runtime["invoice_id"])

    runtime["payment"].inspect_payment = unreachable
    worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))

    after = runtime["store"].get_payment(runtime["invoice_id"])
    assert after["fee_units"] is None
    assert after["erp_error_code"] == "NETWORK_FEE_UNAVAILABLE"
    assert after["transaction_hash"] == before["transaction_hash"]
    assert runtime["store"].get_state(runtime["invoice_id"]) == WorkflowState.ERP_PENDING.value
