"""The stand-in ERPNext answers the connector's real requests, so the demo path runs end to end."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from arc_payables import sim_erpnext
from arc_payables.frappe_adapter import FrappeAccountingConnector
from arc_payables.ports import PaymentMapping
from arc_payables.settings import Settings

PINV_CLEAN = "ACC-PINV-2026-00101"
PINV_UNVERIFIED = "ACC-PINV-2026-00102"
TX = "0x" + "ab" * 32


@pytest.fixture()
def erp():
    app = sim_erpnext.create_app()
    return app, TestClient(app, base_url="http://sim-erpnext.test")


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        frappe_url="http://sim-erpnext.test",
        frappe_api_key="demo-key",
        frappe_api_secret="demo-secret",
        frappe_company=sim_erpnext.COMPANY,
        frappe_paid_from_account=sim_erpnext.PAID_FROM,
        frappe_paid_to_account=sim_erpnext.PAID_TO,
        frappe_mode_of_payment=sim_erpnext.MODE_OF_PAYMENT,
        frappe_settlement_currency="USDC",
        frappe_company_currency="USD",
        frappe_invoice_currency="USD",
        frappe_source_exchange_rate=Decimal("1"),
        frappe_target_exchange_rate=Decimal("1"),
        frappe_fee_account=sim_erpnext.FEE_ACCOUNT,
        frappe_cost_center=sim_erpnext.COST_CENTER,
        frappe_fee_currency="USD",
    )


def _mapping() -> PaymentMapping:
    return PaymentMapping(
        company=sim_erpnext.COMPANY, paid_from=sim_erpnext.PAID_FROM, paid_to=sim_erpnext.PAID_TO,
        mode_of_payment=sim_erpnext.MODE_OF_PAYMENT, source_currency="USDC", target_currency="USD",
        company_currency="USD", invoice_currency="USD", source_exchange_rate="1", target_exchange_rate="1",
        fee_account=sim_erpnext.FEE_ACCOUNT, cost_center=sim_erpnext.COST_CENTER, fee_currency="USD",
    )


def test_both_seeded_bills_are_open_payables(erp):
    _, client = erp
    connector = FrappeAccountingConnector(_settings(), client)
    assert sorted(connector.list_open_payables()) == [PINV_CLEAN, PINV_UNVERIFIED]


def test_a_bill_imports_with_its_order_receipt_and_supplier(erp):
    _, client = erp
    invoice, evidence = FrappeAccountingConnector(_settings(), client).import_invoice(PINV_CLEAN)
    assert invoice.amount_units == 250 * 1_000_000
    assert evidence.supplier and evidence.supplier.wallet_verified
    assert evidence.invoice_status == "UNPAID" or evidence.invoice_status == "SUBMITTED"
    assert evidence.purchase_orders and evidence.receipts


def test_the_second_suppliers_wallet_is_unverified_until_a_person_verifies_it(erp):
    _, client = erp
    connector = FrappeAccountingConnector(_settings(), client)
    _, before = connector.import_invoice(PINV_UNVERIFIED)
    assert before.supplier and not before.supplier.wallet_verified

    assert client.post("/sim/verify-wallet/SUP-NORTHWIND-002", follow_redirects=False).status_code == 303

    _, after = connector.import_invoice(PINV_UNVERIFIED)
    assert after.supplier and after.supplier.wallet_verified


def test_the_writeback_marks_the_bill_paid_and_books_the_fee_once(erp):
    app, client = erp
    connector = FrappeAccountingConnector(_settings(), client)
    invoice, _ = connector.import_invoice(PINV_CLEAN)

    written = connector.create_payment_entry(invoice, TX, _mapping())
    fee = connector.create_fee_expense(invoice, TX, 10_000, _mapping())

    bill = app.state.docs["Purchase Invoice"][PINV_CLEAN]
    assert bill["status"] == "Paid" and bill["outstanding_amount"] == 0
    assert app.state.docs["Payment Entry"][written.payment_entry_id]["docstatus"] == 1
    assert fee is not None and app.state.docs["Journal Entry"][fee.payment_entry_id]["docstatus"] == 1
    assert connector.list_open_payables() == [PINV_UNVERIFIED]

    # A retry finds the entries by transaction reference instead of booking them twice.
    again = connector.create_payment_entry(invoice, TX, _mapping())
    assert again.already_existed and len(app.state.docs["Payment Entry"]) == 1


def test_the_page_says_it_is_a_stand_in_and_lists_the_documents(erp):
    _, client = erp
    page = client.get("/").text
    assert "not ERPNext" in page
    assert "not on any chain" in page
    assert PINV_CLEAN in page and "Verify wallet" in page


def test_a_name_only_submit_is_refused_like_the_real_system(erp):
    _, client = erp
    client.post("/api/resource/Payment Entry", json={"party": "SUP-ACME-001", "paid_amount": 1, "references": []})
    refused = client.post("/api/method/frappe.client.submit", json={"doc": {"doctype": "Payment Entry", "name": "ACC-PAY-2026-00001"}})
    assert refused.status_code == 417
