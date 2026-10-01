"""The settlement log and the report an operator reads months later.

The question this answers is "what happened to this payment, and if it failed, why". So the tests are
about the verdict being right in every state the workflow can reach, about finding the record from
whatever identifier an operator happens to hold, and about the report never handing back the
material that could re-authorize a payment.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from arc_payables.audit_log import (
    FAILED,
    NOT_ATTEMPTED,
    SETTLED,
    SETTLED_NOT_RECORDED,
    UNCONFIRMED,
    list_payments,
    payment_report,
    verify_payment,
)
from arc_payables.domain import WorkflowState
from arc_payables.settings import Settings

from test_worker import _extra_eligible_invoice


def _client(runtime) -> TestClient:
    from arc_payables.api import create_app

    settings = Settings(_env_file=None, database_path=runtime["settings"].database_path, api_key="log-key")
    app = create_app(
        settings=settings,
        store=runtime["store"],
        accounting=runtime["accounting"],
        payment_provider=runtime["payment"],
    )
    return TestClient(app)


def _settle(runtime, invoice_id: str, *, fee_units: int = 10_000) -> None:
    """Settle one invoice through the mock, with a measured fee so the fee entry exists."""
    runtime["payment"].fee_units = fee_units
    runtime["workflow"].evaluate(invoice_id)
    runtime["workflow"].submit_payment(invoice_id)


def _set_payment(runtime, invoice_id: str, updates: dict, state: str) -> None:
    runtime["store"].update_payment(invoice_id, updates, state, "TEST_SETUP")


# --------------------------------------------------------------------------------------
# The verdict
# --------------------------------------------------------------------------------------


def test_a_successful_payment_says_so_and_names_both_ledger_entries(runtime):
    _settle(runtime, runtime["legitimate_id"])

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert report["outcome"]["code"] == SETTLED
    assert report["outcome"]["success"] is True
    assert report["ledger"]["payment_entry"]
    assert report["ledger"]["fee_entry"]
    assert report["settlement"]["transaction_hash"].startswith("0x")
    assert report["settlement"]["explorer_url"].endswith(report["settlement"]["transaction_hash"])
    assert report["audit"]["ok"] is True


def test_money_that_moved_without_a_ledger_entry_is_not_reported_as_success(runtime):
    _settle(runtime, runtime["legitimate_id"])
    _set_payment(
        runtime,
        runtime["legitimate_id"],
        {"erp_status": "DISABLED", "erp_error_code": "NETWORK_FEE_UNAVAILABLE"},
        WorkflowState.ERP_PENDING.value,
    )

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert report["outcome"]["code"] == SETTLED_NOT_RECORDED
    assert report["outcome"]["success"] is False
    assert report["ledger"]["error_code"] == "NETWORK_FEE_UNAVAILABLE"
    assert "ledger has not taken it" in report["outcome"]["summary"]


def test_an_uncertain_settlement_says_reconcile_and_never_resend(runtime):
    _settle(runtime, runtime["legitimate_id"])
    _set_payment(runtime, runtime["legitimate_id"], {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert report["outcome"]["code"] == UNCONFIRMED
    assert "never resend" in report["outcome"]["summary"]


def test_a_failed_payment_reports_the_failure_code(runtime):
    _settle(runtime, runtime["legitimate_id"])
    _set_payment(
        runtime,
        runtime["legitimate_id"],
        {"confirmation_status": "FAILED", "failure_code": "GUARD_PER_PAYMENT_CAP_EXCEEDED"},
        WorkflowState.FAILED.value,
    )

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert report["outcome"]["code"] == FAILED
    assert report["outcome"]["reason"] == "GUARD_PER_PAYMENT_CAP_EXCEEDED"


def test_an_invoice_nothing_happened_to_is_reported_as_not_attempted(runtime):
    report = payment_report(runtime["workflow"], runtime["suspicious_id"])

    assert report["outcome"]["code"] == NOT_ATTEMPTED
    assert report["settlement"]["transaction_hash"] is None
    assert report["authorization"]["authorized"] is False


# --------------------------------------------------------------------------------------
# Finding the record
# --------------------------------------------------------------------------------------


def test_the_report_is_reachable_from_any_identifier_an_operator_holds(runtime):
    _settle(runtime, runtime["legitimate_id"])
    stored = runtime["store"].get_payment(runtime["legitimate_id"])

    for reference, expected in (
        (runtime["legitimate_id"], "invoice_id"),
        (stored["transaction_hash"], "transaction_hash"),
        (stored["transaction_hash"].upper(), "transaction_hash"),
        (stored["payment_id"], "payment_id"),
        (stored["permit"]["payment_id"], "payment_id"),
    ):
        report = payment_report(runtime["workflow"], reference)
        assert report["reference"]["matched_by"] == expected, reference
        assert report["invoice"]["id"] == runtime["legitimate_id"], reference


def test_the_transaction_hash_is_not_the_payment_id(runtime):
    """The double used to derive one from the other, which made a lookup bug invisible."""
    _settle(runtime, runtime["legitimate_id"])
    stored = runtime["store"].get_payment(runtime["legitimate_id"])

    assert stored["transaction_hash"] != stored["payment_id"]


def test_an_unknown_reference_is_a_404_with_a_code(runtime):
    client = _client(runtime)
    response = client.get("/payments/does-not-exist", headers={"X-API-Key": "log-key"})

    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "payment_not_found"
    with pytest.raises(LookupError):
        payment_report(runtime["workflow"], "does-not-exist")


# --------------------------------------------------------------------------------------
# What the report must never hand back
# --------------------------------------------------------------------------------------


def test_the_report_never_returns_reusable_authorization_material(runtime):
    _settle(runtime, runtime["legitimate_id"])
    signature = runtime["store"].get_payment(runtime["legitimate_id"])["signature"]

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert signature not in str(report)
    assert "signature" not in report["settlement"]
    # The reader still gets the useful fact: the permit verifies against the configured signer.
    assert report["authorization"]["signature_verified"] is True
    assert report["authorization"]["signer_address"]


def test_the_report_carries_the_integrity_verdict_of_the_chain(runtime):
    _settle(runtime, runtime["legitimate_id"])
    with sqlite3.connect(runtime["settings"].database_path) as connection:
        connection.execute("UPDATE audit_events SET payload_json='{}' WHERE id=1")

    report = payment_report(runtime["workflow"], runtime["legitimate_id"])

    assert report["audit"]["ok"] is False
    assert report["audit"]["reason"]


# --------------------------------------------------------------------------------------
# The log
# --------------------------------------------------------------------------------------


def test_the_log_counts_each_outcome_and_totals_the_value(runtime):
    _settle(runtime, runtime["legitimate_id"])
    _settle(runtime, _extra_eligible_invoice(runtime))
    uncertain = next(
        invoice.id for invoice, _state in runtime["store"].list_invoices()
        if runtime["store"].get_payment(invoice.id) and invoice.id != runtime["legitimate_id"]
    )
    _set_payment(runtime, uncertain, {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    log = list_payments(runtime["workflow"])

    assert log["count"] == 2
    assert log["totals"]["settled"] == 1
    assert log["totals"]["unconfirmed"] == 1
    assert Decimal(log["totals"]["value_usdc"]) > 0
    # Newest first, and every row carries its own verdict rather than leaving it to the reader.
    assert all(row["outcome"]["code"] for row in log["payments"])


def test_the_log_totals_a_fractional_amount_exactly(runtime):
    """A nano payment is the case that broke the first version: a total summed from rendered strings."""
    from datetime import date, timedelta

    from arc_payables import seed
    from arc_payables.domain import InvoiceLine

    payable = seed._erp_payable(
        external_id="PINV-NANO-1",
        invoice_number="NANO-1",
        amount_units=10_000,
        invoice_currency="USD",
        invoice_date=date.today() - timedelta(days=1),
        due_date=date.today(),
        lines=(InvoiceLine("INDUSTRIAL-FILTER", "1", 10_000, "POL-ACME-2026-001-1", ("PRL-ACME-2026-001-1",)),),
        purchase_order_ids=("PO-ACME-2026-001",),
        receipt_ids=("PR-ACME-2026-001",),
    )
    runtime["store"].seed_fixture("erp_payable", payable["invoice_id"], payable)
    invoice = seed._local_invoice(payable, Decimal("1"), payee_address=seed.APPROVED_WALLET)
    created, _ = runtime["workflow"].create_invoice(invoice, "nano-1")
    _settle(runtime, created.id, fee_units=2_964)

    log = list_payments(runtime["workflow"])

    assert Decimal(log["totals"]["value_usdc"]) == Decimal("0.01")
    assert Decimal(log["totals"]["fees_usdc"]) == Decimal("0.002964")
    assert log["payments"][0]["amount_usdc"] == "0.01"


def test_the_log_filters_by_confirmation_and_ledger_status(runtime):
    _settle(runtime, runtime["legitimate_id"])
    _settle(runtime, _extra_eligible_invoice(runtime))
    uncertain = next(
        invoice.id for invoice, _state in runtime["store"].list_invoices()
        if runtime["store"].get_payment(invoice.id) and invoice.id != runtime["legitimate_id"]
    )
    _set_payment(runtime, uncertain, {"confirmation_status": "UNCERTAIN", "erp_status": "PENDING"}, WorkflowState.NEEDS_RECONCILIATION.value)

    assert list_payments(runtime["workflow"], confirmation="CONFIRMED")["count"] == 1
    assert list_payments(runtime["workflow"], confirmation="UNCERTAIN")["count"] == 1
    assert list_payments(runtime["workflow"], ledger="RECORDED")["count"] == 1


def test_a_payment_that_never_happened_is_absent_from_the_log(runtime):
    log = list_payments(runtime["workflow"])

    assert log["count"] == 0
    assert log["payments"] == []
    assert log["totals"]["value_usdc"] == "0"


# --------------------------------------------------------------------------------------
# The live re-check
# --------------------------------------------------------------------------------------


def test_the_recheck_asks_the_provider_again_without_changing_anything(runtime):
    _settle(runtime, runtime["legitimate_id"])
    before = runtime["store"].get_payment(runtime["legitimate_id"])

    outcome = verify_payment(runtime["workflow"], runtime["legitimate_id"])

    assert outcome["checked"] is True
    assert outcome["provider_status"] == "CONFIRMED"
    assert outcome["agrees_with_record"] is True
    assert runtime["store"].get_payment(runtime["legitimate_id"]) == before


def test_the_recheck_reports_a_disagreeing_fee_rather_than_hiding_it(runtime):
    _settle(runtime, runtime["legitimate_id"], fee_units=10_000)
    # A record that disagrees with the chain: the booked fee is what a human typed, or an old bug.
    _set_payment(runtime, runtime["legitimate_id"], {"fee_units": 999}, WorkflowState.ERP_RECORDED.value)

    outcome = verify_payment(runtime["workflow"], runtime["legitimate_id"])

    assert outcome["fee_units_recorded"] == 999
    assert outcome["fee_units_reported_now"] == 10_000
    assert outcome["fee_agrees"] is False


def test_a_provider_that_cannot_answer_is_reported_not_raised(runtime, monkeypatch):
    _settle(runtime, runtime["legitimate_id"])

    def broken(payment):
        raise RuntimeError("the rpc is down")

    monkeypatch.setattr(runtime["payment"], "inspect_payment", broken)

    outcome = verify_payment(runtime["workflow"], runtime["legitimate_id"])

    assert outcome["checked"] is False
    assert "the rpc is down" in outcome["detail"]


def test_the_audit_endpoints_need_the_key_and_are_documented(runtime):
    _settle(runtime, runtime["legitimate_id"])
    client = _client(runtime)

    assert client.get("/payments").status_code == 401
    assert client.get("/payments/x").status_code == 401
    assert client.post("/payments/x/verify").status_code == 401
    assert client.get("/payments", headers={"X-API-Key": "log-key"}).status_code == 200
    report = client.get(f"/payments/{runtime['legitimate_id']}", headers={"X-API-Key": "log-key"}).json()
    assert report["outcome"]["code"] == SETTLED
    spec = client.get("/openapi.json").json()
    assert "/payments" in spec["paths"] and "/payments/{reference}/verify" in spec["paths"]
