from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from urllib.parse import unquote

import httpx
import pytest

from arc_payables.accounting import (
    AccountingMappingError,
    compute_fee_expense,
    compute_payment_entry_amounts,
    payload_fields,
)
from arc_payables.domain import InvoiceRecord, USDC_SCALE
from arc_payables.frappe_adapter import FrappeAccountingConnector, FrappeAdapterError
from arc_payables.ports import PaymentMapping
from arc_payables.settings import Settings

COMPANY = "Arc Demo Inc"
SETTLEMENT_ACCOUNT = "USDC Wallet - AD"
PAYABLE_ACCOUNT = "Accounts Payable - AD"
FEE_ACCOUNT = "Network Fees - AD"
COST_CENTER = "Main - AD"


def _mapping(**overrides) -> PaymentMapping:
    values = {
        "company": COMPANY,
        "paid_from": SETTLEMENT_ACCOUNT,
        "paid_to": PAYABLE_ACCOUNT,
        "mode_of_payment": "Arc Testnet",
        "source_currency": "USDC",
        "target_currency": "USD",
        "company_currency": "USD",
        "invoice_currency": "USD",
        "source_exchange_rate": "1",
        "target_exchange_rate": "1",
        "fee_account": FEE_ACCOUNT,
        "cost_center": COST_CENTER,
        "fee_currency": "USD",
    }
    values.update(overrides)
    return PaymentMapping(**values)


def _settings(**overrides) -> Settings:
    values = {
        "_env_file": None,
        "frappe_url": "https://erp.example.test",
        "frappe_api_key": "not-a-real-key",
        "frappe_api_secret": "not-a-real-secret",
        "frappe_company": COMPANY,
        "frappe_paid_from_account": SETTLEMENT_ACCOUNT,
        "frappe_paid_to_account": PAYABLE_ACCOUNT,
        "frappe_mode_of_payment": "Arc Testnet",
        "frappe_settlement_currency": "USDC",
        "frappe_company_currency": "USD",
        "frappe_invoice_currency": "USD",
        "frappe_source_exchange_rate": Decimal("1"),
        "frappe_target_exchange_rate": Decimal("1"),
        "frappe_fee_account": FEE_ACCOUNT,
        "frappe_cost_center": COST_CENTER,
        "frappe_fee_currency": "USD",
    }
    values.update(overrides)
    return Settings(**values)


def _invoice(**overrides) -> InvoiceRecord:
    values = {
        "id": "local-invoice-1",
        "supplier_id": "SUP-1",
        "invoice_number": "SUP-INV-1",
        "invoice_date": date(2026, 1, 1),
        "due_date": date(2026, 1, 10),
        "amount_units": 250 * USDC_SCALE,
        "currency": "USDC",
        "invoice_payee_address": "0x1111111111111111111111111111111111111111",
        "lines": (),
        "purchase_invoice_id": "PINV-1",
    }
    values.update(overrides)
    return InvoiceRecord(**values)


def _path(request: httpx.Request) -> str:
    return unquote(request.url.path)


# --------------------------------------------------------------------------------------
# The fee is absorbed, never netted off the supplier's payment
# --------------------------------------------------------------------------------------


def test_payment_entry_settles_the_invoice_in_full():
    amounts = compute_payment_entry_amounts(
        1_000 * USDC_SCALE,
        source_currency="USDC",
        target_currency="USD",
        company_currency="USD",
        source_exchange_rate="1",
        target_exchange_rate="1",
    )
    assert amounts.supplier_amount == Decimal(1000)
    assert amounts.paid_amount == Decimal(1000)
    assert amounts.allocated_amount == Decimal(1000)
    assert amounts.received_amount == Decimal(1000)
    assert amounts.difference_amount == 0
    assert amounts.unallocated_amount == 0


def test_no_network_fee_appears_in_the_payment_entry_arithmetic():
    """The fee is not a parameter here at all: it cannot reach the supplier's amount."""
    without_fee = compute_payment_entry_amounts(
        250 * USDC_SCALE, source_currency="USDC", target_currency="USD",
        company_currency="USD", source_exchange_rate="1", target_exchange_rate="1",
    )
    # Even a 5 USDC fee leaves every figure on this document unchanged; it is booked elsewhere.
    assert without_fee.supplier_amount == Decimal(250)
    assert without_fee.allocated_amount == Decimal(250)
    assert without_fee.paid_amount == Decimal(250)
    assert without_fee.difference_amount == 0


def test_conversion_applies_to_the_invoice_and_still_balances():
    amounts = compute_payment_entry_amounts(
        100 * USDC_SCALE, source_currency="USDC", target_currency="USD",
        company_currency="USD", source_exchange_rate="2", target_exchange_rate="1",
    )
    assert amounts.supplier_amount == Decimal(100)
    assert amounts.allocated_amount == Decimal(200)
    assert amounts.paid_amount == Decimal(100)
    assert amounts.base_paid_amount == Decimal(200)
    assert amounts.base_received_amount == Decimal(200)
    assert amounts.difference_amount == 0


def test_mapping_rejects_a_payable_account_outside_the_company_currency():
    with pytest.raises(AccountingMappingError, match="must equal the company currency"):
        compute_payment_entry_amounts(
            100 * USDC_SCALE, source_currency="USDC", target_currency="USDC",
            company_currency="USD", source_exchange_rate="1", target_exchange_rate="1",
        )


def test_mapping_rejects_non_positive_rates_and_amounts():
    for kwargs, message in (
        ({"source_exchange_rate": "0"}, "positive"),
        ({"target_exchange_rate": "-1"}, "positive"),
        ({"amount_units": 0}, "positive"),
        # A rate on a payable account that is already in the company currency manufactures an
        # exchange gain/loss row, because ERPNext books base_paid - base_received as one.
        ({"target_exchange_rate": "2"}, "target rate of 1"),
    ):
        call = {
            "amount_units": 100 * USDC_SCALE,
            "source_currency": "USDC",
            "target_currency": "USD",
            "company_currency": "USD",
            "source_exchange_rate": "1",
            "target_exchange_rate": "1",
        }
        call.update(kwargs)
        with pytest.raises(AccountingMappingError, match=message):
            compute_payment_entry_amounts(**call)


def test_payload_never_carries_deductions():
    """ERPNext subtracts a deduction from what the party receives, so this document has none."""
    amounts = compute_payment_entry_amounts(
        250 * USDC_SCALE, source_currency="USDC", target_currency="USD",
        company_currency="USD", source_exchange_rate="1", target_exchange_rate="1",
    )
    payload = payload_fields(amounts, "PINV-1")
    assert "deductions" not in payload
    assert payload["paid_amount"] == 250
    assert payload["received_amount"] == 250
    assert payload["references"][0]["allocated_amount"] == 250


def test_fee_expense_rounds_up_to_the_smallest_bookable_unit():
    """A sub-unit fee cannot be written down, so it is booked up and the difference is stated."""
    amounts = compute_fee_expense(
        1,  # one micro-USDC, a thousandth of a cent
        company_currency="USD",
        source_currency="USDC",
        smallest_unit=Decimal("0.01"),
        tx_hash="0x" + "ab" * 32,
        supplier_amount_units=250 * USDC_SCALE,
    )
    assert amounts.measured_amount == Decimal("0.000001")
    assert amounts.booked_amount == Decimal("0.01")
    assert "rounded up" in amounts.remark
    assert "0.000001" in amounts.remark and "0.01" in amounts.remark
    assert "250" in amounts.remark
    assert "0x" + "ab" * 32 in amounts.remark
    # Never rounded down, whatever the fee.
    exact = compute_fee_expense(
        5_000_000, company_currency="USD", source_currency="USDC", smallest_unit=Decimal("0.01"),
        tx_hash="0x" + "ab" * 32, supplier_amount_units=250 * USDC_SCALE,
    )
    assert exact.booked_amount == Decimal("5")
    assert "rounded up" not in exact.remark


# --------------------------------------------------------------------------------------
# Live connector behaviour
# --------------------------------------------------------------------------------------


def test_api_user_is_verified_and_administrator_is_rejected():
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(_path(request))
        return httpx.Response(200, json={"message": "Administrator"})

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FrappeAdapterError, match="Administrator credentials"):
        connector.get_document("Supplier", "SUP-1")
    assert requested == ["/api/method/frappe.auth.get_logged_user"]


def test_discovery_asks_the_ledger_for_what_is_still_owed():
    """Discovery filters on the fact, not on a business status name that can be renamed."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Purchase Invoice":
            seen["filters"] = json.loads(request.url.params["filters"])
            seen["fields"] = json.loads(request.url.params["fields"])
            return httpx.Response(200, json={"data": [{"name": "ACC-PINV-1"}, {"name": "ACC-PINV-2"}]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))

    assert connector.list_open_payables() == ["ACC-PINV-1", "ACC-PINV-2"]
    # A draft, a cancelled invoice or a settled one cannot satisfy this filter.
    assert seen["filters"] == [["docstatus", "=", 1], ["outstanding_amount", ">", 0]]
    assert seen["fields"] == ["name"]


def test_discovery_is_empty_while_the_connector_is_unconfigured():
    connector = FrappeAccountingConnector(_settings(frappe_api_secret=None))
    assert connector.configured is False
    assert connector.list_open_payables() == []


def _erp_invoice_doc(currency: str = "USD", grand_total: float = 250, item_amount: float = 250) -> dict:
    return {
        "name": "PINV-1", "supplier": "SUP-1", "bill_no": "SUP-INV-1", "bill_date": "2026-01-01",
        "posting_date": "2026-01-01", "due_date": "2026-01-10", "currency": currency,
        "grand_total": grand_total, "status": "Unpaid", "docstatus": 1,
        "items": [{
            "item_code": "FILTER", "qty": 10, "amount": item_amount, "po_detail": "POL-1",
            "purchase_order": "PO-1", "pr_detail": "PRL-1", "purchase_receipt": "PR-1",
        }],
    }


def test_import_converts_the_erp_payable_into_the_settlement_amount():
    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Purchase Invoice/PINV-1":
            return httpx.Response(200, json={"data": _erp_invoice_doc()})
        if path == "/api/resource/Supplier/SUP-1":
            return httpx.Response(200, json={"data": {
                "name": "SUP-1", "supplier_name": "Acme",
                "custom_usdc_wallet_address": "0x1111111111111111111111111111111111111111",
                "custom_usdc_wallet_verified": 1, "modified": "2026-01-02",
            }})
        if path == "/api/resource/Purchase Order/PO-1":
            return httpx.Response(200, json={"data": {"name": "PO-1", "supplier": "SUP-1", "status": "Submitted", "docstatus": 1, "items": [{"name": "POL-1", "item_code": "FILTER", "qty": 10, "amount": 250}]}})
        if path == "/api/resource/Purchase Receipt/PR-1":
            return httpx.Response(200, json={"data": {"name": "PR-1", "supplier": "SUP-1", "status": "Completed", "docstatus": 1, "items": [{"name": "PRL-1", "item_code": "FILTER", "qty": 10, "purchase_order_item": "POL-1"}]}})
        if path == "/api/resource/Purchase Invoice":
            return httpx.Response(200, json={"data": [{"name": "PINV-1"}]})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    invoice, evidence = connector.import_invoice("PINV-1")
    # The ERP payable is USD 250; settlement is USDC at the configured rate of 1.
    assert invoice.currency == "USDC"
    assert invoice.amount_units == 250 * USDC_SCALE
    assert invoice.lines[0].amount_units == 250 * USDC_SCALE
    # The ERP's own record stays in accounting currency for the source-of-truth comparison.
    assert evidence.source_invoice_currency == "USD"
    assert evidence.source_invoice_amount_units == 250 * USDC_SCALE
    assert evidence.supplier and evidence.supplier.wallet_verified
    assert evidence.purchase_orders[0].lines[0].id == "POL-1"
    assert evidence.receipts[0].lines[0].purchase_order_line_id == "POL-1"
    assert evidence.duplicate_invoice_id is None


def test_import_refuses_a_rate_that_does_not_divide_exactly():
    handler_doc = _erp_invoice_doc(grand_total=100.01, item_amount=100.01)

    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Purchase Invoice/PINV-1":
            return httpx.Response(200, json={"data": handler_doc})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    connector = FrappeAccountingConnector(
        _settings(frappe_source_exchange_rate=Decimal("3")),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(FrappeAdapterError, match="does not convert exactly"):
        connector.import_invoice("PINV-1")


def test_import_refuses_an_erp_currency_that_is_not_the_configured_invoice_currency():
    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Purchase Invoice/PINV-1":
            return httpx.Response(200, json={"data": _erp_invoice_doc(currency="EUR")})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FrappeAdapterError, match="not the configured invoice currency"):
        connector.import_invoice("PINV-1")


def _payment_handler(
    entries: dict[str, dict],
    counters: dict[str, int],
    *,
    fee_entries: dict[str, dict] | None = None,
    timeout_after_create: bool = False,
    smallest_currency_fraction: float = 0.01,
    settlement_account_currency: str = "USDC",
    payable_account_currency: str = "USD",
    fee_account_currency: str = "USD",
):
    fee_entries = fee_entries if fee_entries is not None else {}
    account_currencies = {
        SETTLEMENT_ACCOUNT: settlement_account_currency,
        PAYABLE_ACCOUNT: payable_account_currency,
        FEE_ACCOUNT: fee_account_currency,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if request.method == "GET" and path == "/api/resource/Payment Entry":
            return httpx.Response(200, json={"data": [{"name": name} for name in entries]})
        if request.method == "GET" and path == "/api/resource/Journal Entry":
            return httpx.Response(200, json={"data": [{"name": name} for name in fee_entries]})
        if path.startswith("/api/resource/Journal Entry/"):
            return httpx.Response(200, json={"data": fee_entries[path.rsplit("/", 1)[-1]]})
        if path.startswith("/api/resource/Payment Entry/"):
            return httpx.Response(200, json={"data": entries[path.rsplit("/", 1)[-1]]})
        if path == "/api/resource/Purchase Invoice/PINV-1":
            return httpx.Response(200, json={"data": {"name": "PINV-1", "currency": "USD"}})
        if path.startswith("/api/resource/Cost Center/"):
            return httpx.Response(200, json={"data": {"name": path.rsplit("/", 1)[-1], "company": COMPANY, "is_group": 0}})
        if path.startswith("/api/resource/Currency/"):
            return httpx.Response(200, json={"data": {
                "name": path.rsplit("/", 1)[-1],
                "smallest_currency_fraction_value": smallest_currency_fraction,
            }})
        if path.startswith("/api/resource/Account/"):
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(200, json={"data": {
                "name": name,
                "company": COMPANY,
                "account_currency": account_currencies.get(name, "USD"),
                "is_group": 0,
            }})
        if request.method == "POST" and path == "/api/resource/Payment Entry":
            counters["create"] += 1
            payload = json.loads(request.content)
            entries["PE-1"] = {**payload, "name": "PE-1", "docstatus": 0,
                               "modified": "2026-01-15 10:00:00.000000"}
            if timeout_after_create and counters["create"] == 1:
                raise httpx.ReadTimeout("response lost after commit", request=request)
            # Real Frappe returns the stored document, not just its name.
            return httpx.Response(200, json={"data": entries["PE-1"]})
        if request.method == "POST" and path == "/api/resource/Journal Entry":
            counters["fee_create"] += 1
            payload = json.loads(request.content)
            fee_entries["JV-1"] = {**payload, "name": "JV-1", "docstatus": 0,
                                   "modified": "2026-01-15 10:05:00.000000"}
            if timeout_after_create and counters["fee_create"] == 1:
                raise httpx.ReadTimeout("response lost after fee commit", request=request)
            return httpx.Response(200, json={"data": fee_entries["JV-1"]})
        if request.method == "POST" and path == "/api/method/frappe.client.submit":
            counters["submit"] += 1
            body = json.loads(request.content)["doc"]
            # Mirror the live v15 contract: a name-only payload reinstantiates an empty document
            # and submits nothing, so it must be rejected.
            required = ("accounts", "user_remark") if body.get("doctype") == "Journal Entry" else ("paid_amount", "references")
            if not body.get("modified") or any(not body.get(field) for field in required):
                return httpx.Response(417, json={"_server_messages": json.dumps([json.dumps({"message": "A full document is required to submit"})])})
            target = fee_entries["JV-1"] if body.get("doctype") == "Journal Entry" else entries["PE-1"]
            target["docstatus"] = 1
            return httpx.Response(200, json={"message": target})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return handler


def test_submit_sends_the_full_document_and_requires_confirmation():
    """A name-only submit silently does nothing in Frappe v15; the whole document must be sent."""
    seen: list[dict] = []
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    base = _payment_handler(entries, counters)

    def handler(request: httpx.Request) -> httpx.Response:
        response = base(request)
        if request.url.path == "/api/method/frappe.client.submit":
            seen.append(json.loads(request.content)["doc"])
        return response

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    connector.create_payment_entry(_invoice(), "0x" + "ab" * 32, _mapping())
    assert len(seen) == 1
    submitted = seen[0]
    assert submitted["doctype"] == "Payment Entry"
    assert submitted["name"] == "PE-1"
    assert submitted["paid_amount"] == 250            # exactly the supplier amount
    assert "deductions" not in submitted
    assert submitted["references"][0]["allocated_amount"] == 250
    assert submitted["modified"]                      # concurrency timestamp is required


def test_unconfirmed_submission_is_not_reported_as_completed_writeback():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    base = _payment_handler(entries, counters)

    def handler(request: httpx.Request) -> httpx.Response:
        response = base(request)
        if request.url.path == "/api/method/frappe.client.submit":
            # Server accepts the request but the document is still a draft.
            return httpx.Response(200, json={"message": {**entries["PE-1"], "docstatus": 0}})
        return response

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(FrappeAdapterError):
        connector.create_payment_entry(_invoice(), "0x" + "ab" * 32, _mapping())


def test_payment_entry_carries_exactly_the_supplier_amount():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(), httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters)))
    )
    result = connector.create_payment_entry(_invoice(), "0x" + "ab" * 32, _mapping())
    assert result.payment_entry_id == "PE-1"
    assert result.already_existed is False
    assert counters == {"create": 1, "submit": 1, "fee_create": 0}

    payload = entries["PE-1"]
    assert payload["paid_amount"] == 250            # a number: ERPNext sums this field as-is
    assert payload["received_amount"] == 250        # party amount
    assert payload["references"][0]["allocated_amount"] == 250
    assert "deductions" not in payload
    assert payload["source_exchange_rate"] == "1"
    assert payload["target_exchange_rate"] == "1"

    reused = connector.create_payment_entry(_invoice(), "0x" + "ab" * 32, _mapping())
    assert reused.already_existed is True
    assert counters == {"create": 1, "submit": 1, "fee_create": 0}


def test_network_fee_is_booked_as_its_own_expense_entry():
    entries: dict[str, dict] = {}
    fee_entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, fee_entries=fee_entries))),
    )
    result = connector.create_fee_expense(_invoice(), "0x" + "ab" * 32, 10_000, _mapping())
    assert result is not None and result.payment_entry_id == "JV-1"
    assert counters == {"create": 0, "submit": 1, "fee_create": 1}

    je = fee_entries["JV-1"]
    assert je["cheque_no"] == "ARC-TESTNET-FEE:" + "0x" + "ab" * 32
    assert je["voucher_type"] == "Journal Entry"
    assert je["company"] == COMPANY
    assert je["multi_currency"] == 1          # a USD fee account against a USDC wallet
    debit, credit = je["accounts"]
    assert debit["account"] == FEE_ACCOUNT
    assert debit["debit_in_account_currency"] == 0.01
    assert debit["cost_center"] == COST_CENTER      # ERPNext demands one on an expense row
    assert credit["account"] == SETTLEMENT_ACCOUNT
    assert credit["credit_in_account_currency"] == 0.01
    assert credit["exchange_rate"] == "1"       # the wallet is not in the company currency
    assert "0x" + "ab" * 32 in je["user_remark"]

    reused = connector.create_fee_expense(_invoice(), "0x" + "ab" * 32, 10_000, _mapping())
    assert reused is not None and reused.already_existed is True
    assert counters == {"create": 0, "submit": 1, "fee_create": 1}


def test_no_fee_means_no_expense_entry():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(), httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters)))
    )
    assert connector.create_fee_expense(_invoice(), "0x" + "ab" * 32, 0, _mapping()) is None
    assert counters == {"create": 0, "submit": 0, "fee_create": 0}


def test_an_existing_fee_entry_for_another_amount_is_a_conflict():
    entries: dict[str, dict] = {}
    fee_entries = {
        "JV-1": {
            "name": "JV-1", "docstatus": 1, "company": COMPANY,
            "cheque_no": "ARC-TESTNET-FEE:" + "0x" + "ab" * 32,
            "accounts": [
                {"account": FEE_ACCOUNT, "debit_in_account_currency": 0.02},
                {"account": SETTLEMENT_ACCOUNT, "credit_in_account_currency": 0.02},
            ],
        }
    }
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, fee_entries=fee_entries))),
    )
    with pytest.raises(FrappeAdapterError, match="booked fee"):
        connector.create_fee_expense(_invoice(), "0x" + "ab" * 32, 10_000, _mapping())
    assert counters["fee_create"] == 0


def test_payment_entry_timeout_after_remote_commit_reuses_existing_draft():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, timeout_after_create=True))),
    )
    args = (_invoice(), "0x" + "cd" * 32, _mapping())
    with pytest.raises(FrappeAdapterError) as error:
        connector.create_payment_entry(*args)
    assert error.value.uncertain
    result = connector.create_payment_entry(*args)
    assert result.payment_entry_id == "PE-1"
    assert result.already_existed is True
    assert counters == {"create": 1, "submit": 1, "fee_create": 0}


def test_payment_entry_refuses_a_settlement_account_in_the_wrong_currency():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, settlement_account_currency="EUR"))),
    )
    with pytest.raises(FrappeAdapterError, match="settlement account currency"):
        connector.create_payment_entry(_invoice(), "0x" + "ef" * 32, _mapping())
    assert counters["create"] == 0


def test_payment_entry_refuses_a_payable_account_outside_the_company_currency():
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, payable_account_currency="USDC"))),
    )
    with pytest.raises(FrappeAdapterError, match="payable account currency"):
        connector.create_payment_entry(_invoice(), "0x" + "ef" * 32, _mapping())
    assert counters["create"] == 0


def test_fee_expense_refuses_a_fee_account_outside_the_company_currency():
    # A journal entry against an account in another currency needs a currency and an exchange rate,
    # which this mapping deliberately does not carry, so it is refused by settings and by the check.
    assert not _settings(frappe_fee_currency="USDC").frappe_accounting_ready
    entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, fee_account_currency="USDC"))),
    )
    with pytest.raises(FrappeAdapterError, match="network-fee account currency"):
        connector.create_fee_expense(_invoice(), "0x" + "77" * 32, 10_000, _mapping())
    assert counters["fee_create"] == 0


def test_live_payment_entry_requires_every_mapping_value():
    assert _settings().frappe_accounting_ready
    assert not _settings(frappe_fee_currency=None).frappe_accounting_ready
    assert not _settings(frappe_source_exchange_rate=None).frappe_accounting_ready
    assert not _settings(frappe_source_exchange_rate=Decimal("0")).frappe_accounting_ready
    # A payable, invoice or fee currency outside the company currency is not usable.
    assert not _settings(frappe_company_currency="EUR").frappe_accounting_ready


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ({"docstatus": 1, "status": "Unpaid"}, "UNPAID"),
        ({"docstatus": 1}, "SUBMITTED"),
        ({"docstatus": 0, "status": "Draft"}, "DRAFT"),
        ({"docstatus": 2, "status": "Cancelled"}, "CANCELLED"),
        ({"status": "Unpaid"}, "UNPAID"),
        ({}, "UNKNOWN"),
        ({"docstatus": "not-a-number", "status": "Unpaid"}, "UNKNOWN"),
    ],
)
def test_erp_invoice_status_is_derived_from_docstatus_and_fails_closed(raw, expected):
    assert FrappeAccountingConnector._invoice_status(raw) == expected


def test_invoice_without_linked_purchase_invoice_cannot_claim_payable_state():
    def handler(request: httpx.Request) -> httpx.Response:
        path = _path(request)
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Supplier/SUP-1":
            return httpx.Response(200, json={"data": {"name": "SUP-1", "custom_usdc_wallet_address": "0x1111111111111111111111111111111111111111", "custom_usdc_wallet_verified": 1}})
        if path == "/api/resource/Purchase Invoice":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    connector = FrappeAccountingConnector(_settings(), httpx.Client(transport=httpx.MockTransport(handler)))
    unlinked = _invoice(purchase_invoice_id=None)
    evidence = connector.get_invoice_evidence(unlinked)
    assert evidence.invoice_status == "UNVERIFIED"
    assert evidence.source_invoice_id is None


def test_fee_expense_declares_multi_currency_only_when_the_accounts_differ_in_currency():
    """ERPNext refuses a journal entry that spans currencies unless the entry says it does."""
    entries: dict[str, dict] = {}
    fee_entries: dict[str, dict] = {}
    counters = {"create": 0, "submit": 0, "fee_create": 0}
    connector = FrappeAccountingConnector(
        _settings(),
        httpx.Client(transport=httpx.MockTransport(_payment_handler(entries, counters, fee_entries=fee_entries))),
    )
    connector.create_fee_expense(_invoice(), "0x" + "ab" * 32, 10_000, _mapping())
    assert fee_entries["JV-1"]["multi_currency"] == 1
    assert fee_entries["JV-1"]["cheque_date"]            # a reference number needs its date

    # A company settling in its own currency has nothing to declare.
    same_currency = _mapping(source_currency="USD")
    entries.clear(); fee_entries.clear()
    connector = FrappeAccountingConnector(
        _settings(frappe_settlement_currency="USD"),
        httpx.Client(transport=httpx.MockTransport(
            _payment_handler(entries, counters, fee_entries=fee_entries, settlement_account_currency="USD")
        )),
    )
    connector.create_fee_expense(_invoice(currency="USD"), "0x" + "cd" * 32, 10_000, same_currency)
    assert "multi_currency" not in fee_entries["JV-1"]
