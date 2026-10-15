from __future__ import annotations

import json
from urllib.parse import unquote

import httpx

from arc_payables.erp_verify import FAIL, OK, SKIPPED, render, verify_settlement
from arc_payables.frappe_adapter import FrappeAccountingConnector
from arc_payables.settings import Settings

TX = "0x" + "ab" * 32


def _connector(world: dict) -> FrappeAccountingConnector:
    handler = _handler(world)
    settings = Settings(
        _env_file=None,
        frappe_url="https://erp.example.test",
        frappe_api_key="not-a-real-key",
        frappe_api_secret="not-a-real-secret",
    )
    return FrappeAccountingConnector(settings, httpx.Client(transport=httpx.MockTransport(handler)))


def _world(**overrides):
    world = {
        "payment": {
            "name": "PE-1", "docstatus": 1, "payment_type": "Pay", "party_type": "Supplier",
            "paid_amount": 250, "received_amount": 250, "deductions": [],
            "references": [{"reference_doctype": "Purchase Invoice", "reference_name": "PINV-1", "allocated_amount": 250}],
        },
        "invoice": {"name": "PINV-1", "docstatus": 1, "status": "Paid", "outstanding_amount": 0},
        "fee": {"name": "JV-1", "docstatus": 1, "total_debit": 0.02, "total_credit": 0.02},
        "gl": {
            "PE-1": [{"debit": 250, "credit": 0}, {"debit": 0, "credit": 250}],
            "JV-1": [{"debit": 0.02, "credit": 0}, {"debit": 0, "credit": 0.02}],
        },
        "gl_status": 200,
    }
    world.update(overrides)
    return world


def _handler(world: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        path = unquote(request.url.path)
        assert request.method == "GET", "the verifier must never write"
        if path == "/api/method/frappe.auth.get_logged_user":
            return httpx.Response(200, json={"message": "ap-agent@example.test"})
        if path == "/api/resource/Payment Entry":
            return httpx.Response(200, json={"data": [{"name": "PE-1"}] if world["payment"] else []})
        if path == "/api/resource/Payment Entry/PE-1":
            return httpx.Response(200, json={"data": world["payment"]})
        if path == "/api/resource/Purchase Invoice/PINV-1":
            return httpx.Response(200, json={"data": world["invoice"]})
        if path == "/api/resource/Journal Entry":
            return httpx.Response(200, json={"data": [{"name": "JV-1"}] if world["fee"] else []})
        if path == "/api/resource/Journal Entry/JV-1":
            return httpx.Response(200, json={"data": world["fee"]})
        if path == "/api/resource/GL Entry":
            if world["gl_status"] != 200:
                return httpx.Response(world["gl_status"], json={})
            voucher = json.loads(request.url.params["filters"])[0][2]
            return httpx.Response(200, json={"data": world["gl"].get(voucher, [])})
        raise AssertionError(f"unexpected request {request.method} {request.url}")

    return handler


def _statuses(report) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


def test_a_complete_settlement_is_consistent():
    report = verify_settlement(_connector(_world()), TX, invoice="PINV-1")
    assert report.ok
    assert report.payment_entry == "PE-1" and report.fee_entry == "JV-1" and report.purchase_invoice == "PINV-1"
    assert set(_statuses(report).values()) == {OK}
    assert "RESULT: consistent" in render(report)


def test_an_unknown_transaction_fails_at_the_first_check():
    report = verify_settlement(_connector(_world(payment=None)), TX)
    assert not report.ok
    assert [check.name for check in report.checks] == ["payment_entry_found"]


def test_a_draft_payment_entry_and_an_open_invoice_fail():
    world = _world()
    world["payment"] = {**world["payment"], "docstatus": 0}
    world["invoice"] = {**world["invoice"], "status": "Unpaid", "outstanding_amount": 250}
    statuses = _statuses(verify_settlement(_connector(world), TX))
    assert statuses["payment_entry_submitted"] == FAIL
    assert statuses["invoice_settled"] == FAIL


def test_a_deduction_or_amount_mismatch_fails():
    world = _world()
    world["payment"] = {**world["payment"], "received_amount": 249, "deductions": [{"amount": 1}]}
    assert _statuses(verify_settlement(_connector(world), TX))["payment_entry_amounts_agree"] == FAIL


def test_the_wrong_invoice_fails():
    assert _statuses(verify_settlement(_connector(_world()), TX, invoice="PINV-2"))[
        "payment_entry_matches_requested_invoice"
    ] == FAIL


def test_a_missing_fee_journal_fails_unless_accepted():
    strict = verify_settlement(_connector(_world(fee=None)), TX)
    assert _statuses(strict)["fee_entry_found"] == FAIL and not strict.ok
    lenient = verify_settlement(_connector(_world(fee=None)), TX, allow_missing_fee=True)
    assert _statuses(lenient)["fee_entry_found"] == SKIPPED and lenient.ok


def test_an_unbalanced_ledger_fails():
    world = _world()
    world["gl"] = {**world["gl"], "PE-1": [{"debit": 250, "credit": 0}]}
    assert _statuses(verify_settlement(_connector(world), TX))["payment_entry_ledger_balanced"] == FAIL


def test_a_user_who_cannot_read_the_ledger_gets_a_skip_not_a_pass():
    report = verify_settlement(_connector(_world(gl_status=403)), TX)
    statuses = _statuses(report)
    assert statuses["payment_entry_ledger_balanced"] == SKIPPED
    assert statuses["fee_entry_ledger_balanced"] == SKIPPED
    assert report.ok
