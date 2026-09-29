"""A supplier the accounting system has blocked cannot be paid.

This is the one control where the interesting assertion is that nothing can talk past it: not the
policy's own overrides, and not a human approval. A finance team that puts a supplier on hold has
already made the decision, so this system reads it as evidence rather than treating it as an
exception to be cleared.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from arc_payables.domain import WorkflowState
from arc_payables.frappe_adapter import FrappeAccountingConnector
from arc_payables.service import WorkflowError
from arc_payables.settings import Settings

SUPPLIER = "SUP-ACME-001"


def _block_supplier(store, *, reason: str = "the supplier is on hold", blocked: bool = True) -> None:
    fixture = store.get_supplier_fixture(SUPPLIER)
    store.seed_fixture("supplier", SUPPLIER, {**fixture, "payment_blocked": blocked, "blocked_reason": reason})


def test_a_blocked_supplier_holds_the_payment(runtime):
    _block_supplier(runtime["store"])
    result = runtime["workflow"].evaluate(runtime["legitimate_id"])

    assert result["state"] == WorkflowState.HELD.value
    check = next(item for item in result["decision"]["policy_checks"] if item["code"] == "supplier_blocked")
    assert check["passed"] is False
    assert "the supplier is on hold" in check["detail"]
    # The evidence reference points at the record the claim came from.
    assert any(ref["source"] == "trusted_supplier_record" for ref in result["decision"]["evidence"])


def test_a_blocked_supplier_cannot_reach_the_review_path(runtime):
    """Held is not Escalated, so there is no approval form to fill in for it.

    A blocked supplier is not an exception to acknowledge. Review exists for decisions a human
    should weigh, and this is not one of them: the answer is to change the record in the accounting
    system, not to approve past it.
    """
    _block_supplier(runtime["store"])
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    detail = runtime["workflow"].get_invoice(runtime["legitimate_id"])

    check = next(item for item in detail["decision"]["policy_checks"] if item["code"] == "supplier_blocked")
    assert check["requires_human"] is False and check["overridable"] is False
    assert detail["state"] == WorkflowState.HELD.value

    with pytest.raises(WorkflowError) as refused:
        runtime["workflow"].approve(
            runtime["legitimate_id"],
            {
                "reviewer": "ap",
                "approved": True,
                "note": "trying to clear a blocked supplier",
                "acknowledged_checks": ["supplier_blocked"],
            },
        )
    assert refused.value.code == "not_escalated"

    # And the payment path is closed too.
    with pytest.raises(WorkflowError):
        runtime["workflow"].submit_payment(runtime["legitimate_id"])


def test_a_disabled_supplier_is_blocked_too(runtime):
    _block_supplier(runtime["store"], reason="the supplier record is disabled")
    result = runtime["workflow"].evaluate(runtime["legitimate_id"])
    assert result["state"] == WorkflowState.HELD.value
    check = next(item for item in result["decision"]["policy_checks"] if item["code"] == "supplier_blocked")
    assert "disabled" in check["detail"]


def test_the_advisory_layer_agrees_with_the_policy(runtime):
    """A disagreement here is what silently vetoed the reduced-limit path once already."""
    _block_supplier(runtime["store"])
    decision = runtime["workflow"].evaluate(runtime["legitimate_id"])["decision"]

    assert decision["advisory"]["decided_by"] == "heuristics"
    assert any("hold or disabled" in claim for claim in decision["advisory"]["material_claims"])
    assert "supplier_blocked" in decision["advisory"]["observation_codes"]
    assert decision["action"] == "HOLD"


def test_an_unblocked_supplier_is_unaffected(runtime):
    result = runtime["workflow"].evaluate(runtime["legitimate_id"])
    assert result["state"] == WorkflowState.ELIGIBLE.value
    assert all(item["code"] != "supplier_blocked" for item in result["decision"]["policy_checks"])


def test_the_connector_reads_hold_and_disabled_from_the_supplier_record(monkeypatch):
    connector = FrappeAccountingConnector(
        Settings(_env_file=None, frappe_url="http://erp.invalid", frappe_api_key="k", frappe_api_secret="s")
    )

    def supplier_doc(**fields):
        base = {
            "name": "SUP-1",
            "supplier_name": "Supplier One",
            "modified": "2026-01-01 00:00:00",
            "custom_usdc_wallet_address": "0x1111111111111111111111111111111111111111",
            "custom_usdc_wallet_verified": 1,
        }
        return {**base, **fields}

    for fields, blocked, fragment in (
        ({}, False, None),
        ({"on_hold": 1}, True, "on hold"),
        ({"disabled": 1}, True, "disabled"),
        ({"on_hold": 1, "disabled": 1}, True, "on hold"),
    ):
        monkeypatch.setattr(connector, "get_document", lambda doctype, name, fields=fields: supplier_doc(**fields))
        record = connector._supplier("SUP-1")
        assert record.payment_blocked is blocked, fields
        if fragment:
            assert fragment in (record.blocked_reason or ""), fields

    # A string "0" and a missing field both mean "not blocked".
    monkeypatch.setattr(connector, "get_document", lambda doctype, name: supplier_doc(on_hold="0", disabled=0))
    assert connector._supplier("SUP-1").payment_blocked is False
