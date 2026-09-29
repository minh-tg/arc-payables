"""Captured invoice -> ERP linkage -> authorization.

The invariant under test: no invoice becomes payable unless it is linked to an ERPNext
payable record, and the reviewer matching it can never substitute their own amount, payee,
or approval of the captured document for that accounting record.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import asdict, replace
from datetime import date

import pytest

from arc_payables.domain import DecisionAction, InvoiceRecord, USDC_SCALE, WorkflowState
from arc_payables.seed import APPROVED_WALLET, ATTACKER_WALLET, SUPPLIER_ID
from arc_payables.service import WorkflowError

REVIEWER_TOKEN = "review-token-for-linking"
# A seeded ERP payable that has no local capture yet, so linking it is a realistic first match.
UNLINKED_PAYABLE = "PINV-ACME-2026-003"
UNLINKED_PAYABLE_AMOUNT = "120"
UNLINKED_PAYABLE_NUMBER = "ACME-INV-2026-003"


def _captured_invoice(runtime, *, amount: str = "250", number: str | None = None, **changes) -> InvoiceRecord:
    """A captured invoice with no ERPNext link, as it would arrive from OCR/intake."""
    base = runtime["store"].get_invoice(runtime["legitimate_id"])
    overrides = {
        "id": str(uuid.uuid4()),
        "invoice_number": number or f"CAPTURED-{uuid.uuid4().hex[:8]}",
        "invoice_payee_address": APPROVED_WALLET,
        "purchase_invoice_id": None,
        **changes,
    }
    invoice = replace(base, **overrides)
    runtime["store"].create_invoice(invoice, str(uuid.uuid4()))
    return invoice


def _link(runtime, invoice_id: str, purchase_invoice_id: str = UNLINKED_PAYABLE, reviewer: str = "ap-reviewer"):
    return runtime["workflow"].link_to_purchase_invoice(invoice_id, purchase_invoice_id, reviewer)


def test_captured_invoice_is_held_until_it_is_linked_to_an_erp_payable(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime)
    evaluated = runtime["workflow"].evaluate(invoice.id)
    assert evaluated["state"] == WorkflowState.HELD.value
    assert evaluated["decision"]["action"] == DecisionAction.HOLD.value
    assert evaluated["next_actions"][0]["action"] == "link_erp_invoice"
    assert evaluated["next_actions"][0]["requires_human_token"] is True
    with pytest.raises(WorkflowError) as error:
        runtime["workflow"].submit_payment(invoice.id)
    assert error.value.code == "payment_not_eligible"
    assert runtime["payment"].submission_calls == 0


def test_approving_the_captured_document_alone_never_makes_it_payable(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime)
    runtime["workflow"].evaluate(invoice.id)
    # There is nothing reviewable to acknowledge: the missing link is not an exception.
    with pytest.raises(WorkflowError) as error:
        runtime["workflow"].approve(
            invoice.id,
            {"reviewer": "ap-reviewer", "approved": True, "note": "PDF looks fine", "acknowledged_checks": []},
        )
    assert error.value.code == "not_escalated"

    response = runtime["client"].post(
        f"/invoices/{invoice.id}/approval",
        headers={"X-Approval-Token": REVIEWER_TOKEN},
        json={"reviewer": "ap-reviewer", "approved": True, "note": "PDF looks fine", "acknowledged_checks": ["erp_link_required"]},
    )
    # Approval is not even reachable: a held invoice is not an escalated one, so there is no
    # review path that could acknowledge the missing link away.
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "not_escalated"
    assert runtime["store"].get_payment(invoice.id) is None


def test_linking_adopts_the_erp_payable_and_then_authorizes_the_erp_amount(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    # The capture claims far more than the ERP payable recognizes.
    invoice = _captured_invoice(runtime, amount="250", number="CAPTURED-OVERSTATED")
    held = runtime["workflow"].evaluate(invoice.id)
    assert held["decision"]["action"] == DecisionAction.HOLD.value

    linked = _link(runtime, invoice.id)
    assert linked["invoice"]["purchase_invoice_id"] == UNLINKED_PAYABLE
    # The accounting record is adopted wholesale: the overstated capture cannot inflate payment.
    assert linked["invoice"]["amount"] == UNLINKED_PAYABLE_AMOUNT
    assert linked["invoice"]["invoice_number"] == UNLINKED_PAYABLE_NUMBER
    assert linked["decision"]["action"] == DecisionAction.PAY_NOW.value
    assert linked["next_actions"][0]["action"] == "authorize_payment"

    paid = runtime["workflow"].submit_payment(invoice.id)
    assert paid["state"] == WorkflowState.ERP_RECORDED.value
    permit = runtime["store"].get_payment(invoice.id)["permit"]
    assert permit["amount_units"] == 120 * USDC_SCALE
    assert permit["recipient"] == APPROVED_WALLET


def test_linking_records_the_captured_versus_erp_differences_for_audit(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime, number="CAPTURED-MISMATCH", payment_terms="captured terms")
    _link(runtime, invoice.id)
    events = runtime["workflow"].events(invoice.id)
    linked_event = next(event for event in events if event["type"] == "ERP_PAYABLE_LINKED")
    assert linked_event["payload"]["reviewer"] == "ap-reviewer"
    assert linked_event["payload"]["purchase_invoice_id"] == UNLINKED_PAYABLE
    assert linked_event["payload"]["captured_amount_authoritative"] is False
    assert "invoice_number" in linked_event["payload"]["differences"]
    assert linked_event["payload"]["captured"]["invoice_number"] == "CAPTURED-MISMATCH"
    assert "amount_units" in linked_event["payload"]["adopted_fields"]


def test_linking_preserves_the_untrusted_captured_payee_as_evidence(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime, invoice_payee_address=ATTACKER_WALLET)
    linked = _link(runtime, invoice.id)
    # The captured attacker address survives as evidence and keeps the mismatch escalated.
    assert linked["invoice"]["invoice_payee_address"] == ATTACKER_WALLET
    assert linked["decision"]["action"] == DecisionAction.ESCALATE.value
    check = next(item for item in linked["decision"]["policy_checks"] if item["code"] == "payee_mismatch")
    assert not check["passed"]
    with pytest.raises(WorkflowError):
        runtime["workflow"].submit_payment(invoice.id)
    assert runtime["store"].get_payment(invoice.id) is None


def test_a_reviewer_cannot_link_a_capture_to_another_suppliers_payable(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime, supplier_id="SUP-OTHER-999")
    # The ERP payable belongs to SUP-ACME-001, so the link is refused.
    with pytest.raises(WorkflowError) as error:
        _link(runtime, invoice.id)
    assert error.value.code == "supplier_mismatch"
    assert runtime["store"].get_invoice(invoice.id).purchase_invoice_id is None


def test_linking_refuses_an_unknown_payable_and_leaves_the_capture_untouched(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime)
    with pytest.raises(WorkflowError) as error:
        _link(runtime, invoice.id, purchase_invoice_id="PINV-DOES-NOT-EXIST")
    assert error.value.code == "accounting_invoice_not_found"
    assert runtime["store"].get_invoice(invoice.id).purchase_invoice_id is None


def test_linking_is_refused_once_the_invoice_is_settled(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime)
    _link(runtime, invoice.id)
    runtime["workflow"].submit_payment(invoice.id)
    with pytest.raises(WorkflowError) as error:
        _link(runtime, invoice.id, purchase_invoice_id="PINV-ACME-2026-001")
    assert error.value.code == "invoice_already_settled"


def test_linking_refuses_a_number_already_used_by_another_capture(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    first = _captured_invoice(runtime, number="CAPTURE-A")
    second = _captured_invoice(runtime, number="CAPTURE-B")
    _link(runtime, first.id)
    # The first link adopted the ERP invoice number, so a second link to the same payable
    # would create a duplicate payable reference.
    with pytest.raises(WorkflowError) as error:
        _link(runtime, second.id)
    assert error.value.code == "duplicate_invoice_number"


def test_link_endpoint_requires_the_human_token_and_reports_next_actions(runtime):
    runtime["settings"].approval_token = REVIEWER_TOKEN
    invoice = _captured_invoice(runtime)
    runtime["workflow"].evaluate(invoice.id)

    unauthorized = runtime["client"].post(
        f"/invoices/{invoice.id}/link",
        json={"purchase_invoice_id": UNLINKED_PAYABLE, "reviewer": "ap-reviewer"},
    )
    assert unauthorized.status_code == 401
    assert runtime["store"].get_invoice(invoice.id).purchase_invoice_id is None

    authorized = runtime["client"].post(
        f"/invoices/{invoice.id}/link",
        headers={"X-Approval-Token": REVIEWER_TOKEN},
        json={"purchase_invoice_id": UNLINKED_PAYABLE, "reviewer": "ap-reviewer", "note": "matched to ERP"},
    )
    assert authorized.status_code == 200
    body = authorized.json()
    assert body["invoice"]["purchase_invoice_id"] == UNLINKED_PAYABLE
    assert body["next_actions"][0]["action"] == "authorize_payment"


def test_link_endpoint_is_disabled_without_an_approval_credential(runtime):
    invoice = _captured_invoice(runtime)
    response = runtime["client"].post(
        f"/invoices/{invoice.id}/link",
        json={"purchase_invoice_id": UNLINKED_PAYABLE, "reviewer": "ap-reviewer"},
    )
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "human_approval_not_configured"
