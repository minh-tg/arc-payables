"""Exception cases: storage, and the items that name a record to correct.

A case is only ever a description of a decision that already happened. These tests pin the storage
shape it is served with, the record each failing check points at, and the idempotence that keeps two
processes evaluating the same invoice from opening two cases.
"""

from __future__ import annotations

import glob
import pathlib
import re
import uuid
from dataclasses import asdict, replace
from datetime import date, timedelta

from arc_payables import cases
from arc_payables.domain import DecisionAction, InvoiceRecord, WorkflowState
from arc_payables.seed import ATTACKER_WALLET, SUPPLIER_ID


def _seed_erp_payable(runtime, invoice: InvoiceRecord) -> None:
    """Seed the accounting payable this invoice claims to be linked to.

    The payable is a separate record from the invoice, so the policy comparison the case reads from
    is a real comparison rather than the invoice validating itself.
    """
    runtime["store"].seed_fixture(
        "erp_payable",
        invoice.purchase_invoice_id,
        {
            "invoice_id": invoice.purchase_invoice_id,
            "invoice_number": invoice.invoice_number,
            "supplier_id": invoice.supplier_id,
            "amount_units": invoice.amount_units,
            "currency": runtime["settings"].frappe_invoice_currency or "USD",
            "invoice_date": invoice.invoice_date.isoformat(),
            "due_date": invoice.due_date.isoformat(),
            "lines": [asdict(line) for line in invoice.lines],
            "purchase_order_ids": list(invoice.purchase_order_ids),
            "receipt_ids": list(invoice.receipt_ids),
            "payment_terms": invoice.payment_terms,
            "payee_address": None,
            "status": "SUBMITTED",
        },
    )


def _new_invoice(runtime, **changes) -> InvoiceRecord:
    base = runtime["store"].get_invoice(runtime["legitimate_id"])
    invoice = replace(
        base,
        id=str(uuid.uuid4()),
        invoice_number=f"TEST-{uuid.uuid4().hex[:10]}",
        purchase_invoice_id=f"PINV-TEST-{uuid.uuid4().hex[:8]}",
        source_document_hash=uuid.uuid4().hex * 2,
        **changes,
    )
    runtime["store"].create_invoice(invoice, str(uuid.uuid4()))
    _seed_erp_payable(runtime, invoice)
    return invoice


def _checks(*specs) -> list[dict]:
    """Policy check dicts in the shape the decision serialises."""
    return [dict(spec) for spec in specs]


def test_opening_a_case_twice_creates_it_once(runtime):
    store = runtime["store"]
    items = [{"check_code": "missing_receipt", "detail": "No Purchase Receipt evidence was found.",
              "fix": "source_record", "source_type": "Purchase Receipt", "source_id": None,
              "owner_role": "Receiving"}]

    assert store.open_case(runtime["legitimate_id"], "0xabc", items) is True
    assert store.open_case(runtime["legitimate_id"], "0xabc", items) is False

    row = store.case_row(runtime["legitimate_id"])
    assert row["status"] == cases.OPEN
    assert row["fingerprint"] == "0xabc"
    assert row["last_checked_at"] is None
    assert store.case_items(runtime["legitimate_id"]) == items


def test_a_case_keeps_its_own_event_history(runtime):
    store = runtime["store"]
    store.open_case(runtime["legitimate_id"], "0xabc", [])
    store.record_case_event(runtime["legitimate_id"], "case_opened")
    store.record_case_event(runtime["legitimate_id"], "alert_failed", "connection_refused")
    store.set_case_checked(runtime["legitimate_id"], "2026-10-15T09:00:00+00:00")

    events = store.case_events(runtime["legitimate_id"])
    assert [event["type"] for event in events] == ["case_opened", "alert_failed"]
    assert events[1]["detail"] == "connection_refused"
    assert events[0]["at"]
    assert store.case_row(runtime["legitimate_id"])["last_checked_at"] == "2026-10-15T09:00:00+00:00"


def test_an_alert_is_attempted_once(runtime):
    store = runtime["store"]
    store.open_case(runtime["legitimate_id"], "0xabc", [])
    assert [row["invoice_id"] for row in store.cases_awaiting_alert()] == [runtime["legitimate_id"]]

    store.record_case_event(runtime["legitimate_id"], "alert_sent")
    assert store.cases_awaiting_alert() == []


def test_a_cleared_case_is_no_longer_open_but_still_readable(runtime):
    store = runtime["store"]
    store.open_case(runtime["legitimate_id"], "0xabc", [{"check_code": "missing_receipt", "detail": "d",
                                                         "fix": "source_record", "source_type": "Purchase Receipt",
                                                         "source_id": None, "owner_role": "Receiving"}])
    store.replace_case(runtime["legitimate_id"], cases.CLEARED, "0xdef", [])

    row = store.case_row(runtime["legitimate_id"])
    assert row["status"] == cases.CLEARED
    assert store.case_items(runtime["legitimate_id"]) == []
    assert store.open_case_rows() == []
    assert store.cases_awaiting_alert() == []


def test_the_payload_is_the_documented_shape(runtime):
    store = runtime["store"]
    items = cases.case_items(
        _checks({"code": "missing_receipt", "passed": False, "detail": "No Purchase Receipt evidence was found.",
                 "evidence_refs": (), "requires_human": True, "overridable": True}),
        store.get_invoice(runtime["legitimate_id"]),
    )
    store.open_case(runtime["legitimate_id"], "0xabc", items)
    store.record_case_event(runtime["legitimate_id"], "case_opened")

    payload = cases.case_payload(
        store.case_row(runtime["legitimate_id"]),
        store.case_items(runtime["legitimate_id"]),
        store.case_events(runtime["legitimate_id"]),
    )

    assert set(payload) == {"invoice_id", "status", "opened_at", "last_checked_at", "items", "events"}
    assert set(payload["items"][0]) == {"check_code", "detail", "fix", "source_type", "source_id", "owner_role"}
    assert set(payload["events"][0]) == {"type", "at", "detail"}
    assert payload["events"][0]["detail"] is None
    assert cases.case_summary(store.case_row(runtime["legitimate_id"]), store.case_items(runtime["legitimate_id"])) == {
        "status": "open",
        "source_fixes": 0,
        "reviews": 1,
    }


def test_an_item_names_the_record_the_check_read(runtime):
    invoice = runtime["store"].get_invoice(runtime["legitimate_id"])
    items = cases.case_items(
        _checks(
            {"code": "purchase_order_match", "passed": False, "detail": "PO mismatch.", "evidence_refs": ["po:PO-7"],
             "requires_human": True, "overridable": True},
            {"code": "receipt_match", "passed": False, "detail": "Receipt mismatch.", "evidence_refs": ["receipt:PR-9"],
             "requires_human": True, "overridable": True},
            {"code": "payee_mismatch", "passed": False, "detail": "Payee differs.",
             "evidence_refs": ["supplier:SUP-1:wallet", "invoice:x:payee"], "requires_human": True, "overridable": True},
            {"code": "accounting_invoice_match", "passed": False, "detail": "Does not match.",
             "evidence_refs": ["erp:purchase-invoice:PINV-3", "invoice:x:amount"], "requires_human": True,
             "overridable": False},
            {"code": "duplicate_invoice", "passed": False, "detail": "Duplicate.",
             "evidence_refs": ["erp:purchase-invoice:PINV-DUP"], "requires_human": False, "overridable": False},
            {"code": "screening_flagged", "passed": False, "detail": "Flagged.",
             "evidence_refs": ["screening:SUP-1"], "requires_human": True, "overridable": False},
        ),
        invoice,
    )

    assert [(item["source_type"], item["source_id"]) for item in items] == [
        ("Purchase Order", "PO-7"),
        ("Purchase Receipt", "PR-9"),
        ("Supplier", "SUP-1"),
        ("Purchase Invoice", "PINV-3"),
        ("Purchase Invoice", "PINV-DUP"),
        ("Screening result", "SUP-1"),
    ]
    assert items[0]["owner_role"] == "Procurement"
    assert items[1]["owner_role"] == "Receiving"
    assert items[5]["owner_role"] == "Compliance"
    assert [item["fix"] for item in items] == [
        cases.HUMAN_REVIEW, cases.HUMAN_REVIEW, cases.HUMAN_REVIEW, cases.SOURCE_RECORD,
        cases.SOURCE_RECORD, cases.SOURCE_RECORD,
    ]


def test_an_unlinked_invoice_names_no_accounting_record(runtime):
    """A captured invoice with nothing linked has no payable to point at, only a step to take."""
    invoice = runtime["store"].get_invoice(runtime["legitimate_id"])
    item = cases.case_items(
        _checks({"code": "erp_link_required", "passed": False, "detail": "No payable linked.",
                 "evidence_refs": [f"invoice:{invoice.id}", f"erp:purchase-invoice:{invoice.purchase_invoice_id}"],
                 "requires_human": False, "overridable": False}),
        invoice,
    )[0]

    assert item["source_id"] is None
    assert item["fix"] == cases.SOURCE_RECORD


def test_a_treasury_check_names_no_record(runtime):
    invoice = runtime["store"].get_invoice(runtime["legitimate_id"])
    item = cases.case_items(
        _checks({"code": "cash_reserve", "passed": False, "detail": "Reserve breach.",
                 "evidence_refs": ["treasury:snapshot-9", f"invoice:{invoice.id}:amount"],
                 "requires_human": True, "overridable": True}),
        invoice,
    )[0]

    assert (item["source_type"], item["source_id"], item["owner_role"]) == ("Treasury balance", None, "Treasury")


def test_every_policy_check_has_a_record_mapping():
    """A check with no mapping would send an operator to a record that does not exist."""
    text = "\n".join(
        pathlib.Path(path).read_text()
        for path in glob.glob("src/arc_payables/*.py")
        if "explain" not in path
    )
    codes = set(re.findall(r'PolicyCheck\("([a-z_]+)"', text))
    assert codes
    missing = sorted(code for code in codes if code not in cases.RECORDS)
    assert not missing, f"no record mapping for: {missing}"


def test_a_missing_receipt_opens_a_case_naming_the_record_to_create(runtime):
    invoice = _new_invoice(runtime, receipt_ids=())
    result = runtime["workflow"].evaluate(invoice.id)

    assert result["decision"]["action"] == DecisionAction.ESCALATE.value
    assert result["state"] == WorkflowState.ESCALATED.value
    assert runtime["store"].case_row(invoice.id)["status"] == cases.OPEN
    assert runtime["store"].case_items(invoice.id) == [
        {
            "check_code": "missing_receipt",
            "detail": "No Purchase Receipt evidence was found.",
            "fix": cases.HUMAN_REVIEW,
            "source_type": "Purchase Receipt",
            "source_id": None,
            "owner_role": "Receiving",
        }
    ]
    assert [event["type"] for event in runtime["store"].case_events(invoice.id)] == ["case_opened"]


def test_a_payee_mismatch_opens_a_case_naming_the_supplier_record(runtime):
    supplier = runtime["store"].get_supplier_fixture(SUPPLIER_ID)
    supplier["approved_wallet"] = ATTACKER_WALLET
    runtime["store"].seed_fixture("supplier", SUPPLIER_ID, supplier)
    runtime["store"].seed_fixture("screening", ATTACKER_WALLET.lower(), {"status": "CLEAR"})

    runtime["workflow"].evaluate(runtime["legitimate_id"])

    assert runtime["store"].case_items(runtime["legitimate_id"]) == [
        {
            "check_code": "payee_mismatch",
            "detail": "Invoice payee differs from the trusted Supplier wallet; invoice address cannot be a payment destination.",
            "fix": cases.HUMAN_REVIEW,
            "source_type": "Supplier",
            "source_id": SUPPLIER_ID,
            "owner_role": "Accounts payable",
        }
    ]
    # A case describes work; it is not an approval, an authorization or a payment record.
    assert runtime["store"].get_approval(runtime["legitimate_id"]) is None
    assert runtime["store"].get_payment(runtime["legitimate_id"]) is None
    assert runtime["payment"].submission_calls == 0


def test_an_eligible_invoice_has_no_case(runtime):
    runtime["workflow"].evaluate(runtime["legitimate_id"])

    assert runtime["store"].case_row(runtime["legitimate_id"]) is None


def test_a_waiting_invoice_has_no_case(runtime):
    invoice = _new_invoice(runtime, due_date=date.today() + timedelta(days=30))
    result = runtime["workflow"].evaluate(invoice.id)

    assert result["state"] == WorkflowState.WAITING.value
    assert runtime["store"].case_row(invoice.id) is None


def test_repeated_evaluations_do_not_open_a_second_case(runtime):
    invoice = _new_invoice(runtime, receipt_ids=())
    runtime["workflow"].evaluate(invoice.id)
    runtime["workflow"].evaluate(invoice.id)

    assert [event["type"] for event in runtime["store"].case_events(invoice.id)] == ["case_opened"]
