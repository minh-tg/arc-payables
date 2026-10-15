"""Exception cases: the work a held invoice needs, and where the record lives.

An evaluation that ends in ESCALATE or HOLD with a failing check opens a case. The case carries one
item per failing check, naming the accounting record behind it and the role that usually handles
that record, so an operator does not have to reconstruct any of it from the decision. It keeps its
own history: when it opened, whether the alert reached anyone, when the source evidence changed, and
what the next evaluation found.

Nothing here decides anything. A case describes what a decision already decided, and clearing one
means only that the latest evaluation found no failing check. The invoice then continues through the
normal policy, authorization and payment path, or it does not.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .alerting import WARNING, Alert
from .domain import InvoiceRecord

OPEN = "open"
CLEARED = "cleared"
"""A case is open while a check fails, and cleared once the latest evaluation fails nothing."""

SOURCE_RECORD = "source_record"
HUMAN_REVIEW = "human_review"

#: The keys a validated draft carries. A model answer with anything else is rejected rather than
#: partially trusted.
DRAFT_FIELDS = frozenset({"text", "check_code", "source_type", "owner_role"})

#: A URL in any shape a model might produce: a scheme, a bare host, or a markdown link. None of
#: these belongs in a note that a person copies into their own accounting channel.
DRAFT_URL = re.compile(
    r"(https?://|www\.|\[[^\]]*\]\([^)]*\)|\b[a-z0-9][a-z0-9-]*\.(?:com|org|net|io|co|xyz|dev|app)\b)",
    re.IGNORECASE,
)
DRAFT_ADDRESS = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
#: Words that turn an advisory note into an instruction to move money or change authority. The
#: deterministic template uses none of them, so a note that does is replaced by the template.
DRAFT_PAYMENT_WORDS = re.compile(
    r"\b(approve|approval|authorize|authorization|pay|payment|payable|payee|remit|remittance|"
    r"transfer|wire|deposit|disburse|wallet|sign)\b",
    re.IGNORECASE,
)
DRAFT_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")

#: The deterministic note, used when no planner is configured or the model answer is unusable. The
#: text names the record to correct and never carries invoice-derived text, which is untrusted.
DRAFT_TEMPLATE_WITH_RECORD = (
    "To {owner_role}: a check on {source_type} {source_id} is failing for an invoice that is held. "
    "Please review that record and correct it in the accounting system. "
    "The held invoice is checked again on the next worker pass."
)
DRAFT_TEMPLATE_WITHOUT_RECORD = (
    "To {owner_role}: a check that needs a {source_type} record is failing and none was found. "
    "Please create or link one in the accounting system. "
    "The held invoice is checked again on the next worker pass."
)

#: The accounting record behind each check, and the role that usually handles that record. The role
#: is a label for a team, not an assignment to a person, and no user is named anywhere.
RECORDS: dict[str, tuple[str, str]] = {
    "invoice_payable": ("Purchase Invoice", "Accounts payable"),
    "duplicate_invoice": ("Purchase Invoice", "Accounts payable"),
    "duplicate_number_same_supplier": ("Purchase Invoice", "Accounts payable"),
    "amount_outlier": ("Purchase Invoice", "Accounts payable"),
    "invoice_line_total": ("Purchase Invoice", "Accounts payable"),
    "accounting_invoice_match": ("Purchase Invoice", "Accounts payable"),
    "amount_limit": ("Purchase Invoice", "Accounts payable"),
    "settlement_currency": ("Purchase Invoice", "Accounts payable"),
    "erp_link_required": ("Purchase Invoice", "Accounts payable"),
    "payment_timing": ("Purchase Invoice", "Accounts payable"),
    "early_discount": ("Purchase Invoice", "Accounts payable"),
    "supplier_record": ("Supplier", "Accounts payable"),
    "supplier_blocked": ("Supplier", "Accounts payable"),
    "wallet_unverified": ("Supplier", "Accounts payable"),
    "supplier_record_changed": ("Supplier", "Accounts payable"),
    "payee_mismatch": ("Supplier", "Accounts payable"),
    "purchase_order_match": ("Purchase Order", "Procurement"),
    "missing_purchase_order": ("Purchase Order", "Procurement"),
    "receipt_match": ("Purchase Receipt", "Receiving"),
    "missing_receipt": ("Purchase Receipt", "Receiving"),
    "cash_reserve": ("Treasury balance", "Treasury"),
    "treasury_fresh": ("Treasury balance", "Treasury"),
    "address_screening": ("Screening result", "Compliance"),
    "screening_flagged": ("Screening result", "Compliance"),
    "screening_ambiguous": ("Screening result", "Compliance"),
    "screening_unavailable": ("Screening result", "Compliance"),
}

#: Used for a check code this table has not been taught. It names no record, so the console says
#: that no record was found rather than pointing at the wrong one.
DEFAULT_RECORD = ("Accounting record", "Accounts payable")


def case_items(checks: list[dict[str, Any]], invoice: InvoiceRecord) -> list[dict[str, Any]]:
    """One item per failing check, in the order the policy evaluated them."""
    items: list[dict[str, Any]] = []
    for check in checks:
        code = str(check.get("code") or "")
        source_type, owner_role = RECORDS.get(code, DEFAULT_RECORD)
        items.append(
            {
                "check_code": code,
                "detail": str(check.get("detail") or ""),
                "fix": HUMAN_REVIEW if check.get("overridable") and check.get("requires_human") else SOURCE_RECORD,
                "source_type": source_type,
                "source_id": _source_id(code, source_type, check.get("evidence_refs") or (), invoice),
                "owner_role": owner_role,
            }
        )
    return items


def _source_id(code: str, source_type: str, refs: Any, invoice: InvoiceRecord) -> str | None:
    """The accounting record a check points at, or None when there is no record to name.

    The id comes from the check's own evidence references, so the record named is the one the check
    actually read. A linked Purchase Invoice is named by the accounting system's own id; the
    internal invoice id is never offered as a record to correct.
    """
    if code == "erp_link_required" or source_type == "Treasury balance":
        return None
    if source_type == "Purchase Order":
        return _after(refs, "po:")
    if source_type == "Purchase Receipt":
        return _after(refs, "receipt:")
    if source_type == "Screening result":
        return _after(refs, "screening:")
    if source_type == "Supplier":
        for ref in refs:
            if str(ref).startswith("supplier:"):
                return str(ref)[len("supplier:"):].removesuffix(":wallet")
        return None
    if source_type == "Purchase Invoice":
        return _after(refs, "erp:purchase-invoice:") or invoice.purchase_invoice_id
    return None


def _after(refs: Any, prefix: str) -> str | None:
    for ref in refs:
        if str(ref).startswith(prefix):
            return str(ref)[len(prefix):]
    return None


def case_payload(row: dict[str, Any], items: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    """The case as the API returns it. Only these fields, so the console has one shape to read."""
    draft_json = row.get("draft_json")
    return {
        "invoice_id": row["invoice_id"],
        "status": row["status"],
        "opened_at": row["opened_at"],
        "last_checked_at": row["last_checked_at"],
        "items": [
            {
                "check_code": item["check_code"],
                "detail": item["detail"],
                "fix": item["fix"],
                "source_type": item["source_type"],
                "source_id": item["source_id"],
                "owner_role": item["owner_role"],
            }
            for item in items
        ],
        "draft": json.loads(draft_json) if draft_json else None,
        "events": [{"type": event["type"], "at": event["at"], "detail": event["detail"]} for event in events],
    }


def draft_item(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The item a draft is written for: the first record a person can correct, or None.

    A case whose failing checks are all human-review items has no record to name, so it has no
    draft. That keeps the note pointing at an accounting record rather than at a decision.
    """
    for item in items:
        if item.get("fix") == SOURCE_RECORD:
            return item
    return None


def template_draft(item: dict[str, Any]) -> str:
    """The deterministic note for one item. It names the owner and the record, and nothing else."""
    if item.get("source_id"):
        return DRAFT_TEMPLATE_WITH_RECORD.format(
            owner_role=item["owner_role"], source_type=item["source_type"], source_id=item["source_id"]
        )
    return DRAFT_TEMPLATE_WITHOUT_RECORD.format(owner_role=item["owner_role"], source_type=item["source_type"])


def validate_draft(payload: Any, item: dict[str, Any], untrusted: str, max_chars: int) -> tuple[str | None, str | None]:
    """Validate one model answer against the case item. Returns (text, problem), text None if unusable.

    The model may write only the text. It restates the check and record it is writing about, and any
    answer that names a different one is rejected, as is any text carrying a URL, a payment address,
    a money instruction, or a copy of the invoice's own untrusted free text.
    """
    if not isinstance(payload, dict):
        return None, "not_an_object"
    if set(payload) != DRAFT_FIELDS:
        return None, "unknown_fields"
    if payload.get("check_code") != item["check_code"]:
        return None, "unknown_check"
    if payload.get("source_type") != item["source_type"]:
        return None, "unknown_source"
    if payload.get("owner_role") != item["owner_role"]:
        return None, "unknown_role"
    text = payload.get("text")
    if not isinstance(text, str):
        return None, "missing_text"
    text = text.strip()
    if not text:
        return None, "empty"
    if len(text) > max_chars:
        return None, "too_long"
    if DRAFT_CONTROL_CHARS.search(text):
        return None, "not_plain_text"
    if DRAFT_ADDRESS.search(text):
        return None, "payment_address"
    if DRAFT_URL.search(text):
        return None, "url"
    if DRAFT_PAYMENT_WORDS.search(text):
        return None, "payment_instruction"
    if _repeats_invoice_text(text, untrusted):
        return None, "invoice_text_repeated"
    return text, None


def _repeats_invoice_text(text: str, untrusted: str) -> bool:
    """True when the draft copies a run of the invoice's own free text, which is untrusted data."""
    hostile = " ".join(str(untrusted or "").split()).casefold()
    if len(hostile) < 20:
        return False
    plain = " ".join(text.split()).casefold()
    if hostile in plain:
        return True
    return any(plain.find(hostile[start:start + 40]) >= 0 for start in range(0, max(0, len(hostile) - 39), 20))


def case_summary(row: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    """The counts the attention queue shows, split by who can act on each item."""
    return {
        "status": row["status"],
        "source_fixes": sum(1 for item in items if item["fix"] == SOURCE_RECORD),
        "reviews": sum(1 for item in items if item["fix"] == HUMAN_REVIEW),
    }


def add_case_summaries(store: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Attach the case counts to every attention row that lists invoices.

    The attention builder reads the snapshot and knows nothing about cases, so the summary is added
    where the two views meet rather than by teaching the read-only view about another table.
    """
    for item in body.get("items", []):
        for row in (item.get("detail") or {}).get("invoices") or []:
            case = store.case_row(row["invoice_id"])
            row["case"] = case_summary(case, store.case_items(row["invoice_id"])) if case else None
    return body


def case_alert(case: dict[str, Any], items: list[dict[str, Any]], invoice_number: str) -> Alert:
    """One alert for one open case. Ids and counts only: no secret, no configured URL, no prose."""
    records = [item for item in items if item["fix"] == SOURCE_RECORD]
    reviews = [item for item in items if item["fix"] == HUMAN_REVIEW]
    parts: list[str] = []
    if records:
        named = [f"{item['source_type']} {item['source_id']}" if item["source_id"] else item["source_type"] for item in records]
        parts.append(f"{_count(len(records), 'record')} to correct ({', '.join(named)})")
    if reviews:
        parts.append(f"{_count(len(reviews), 'review')} to acknowledge")
    return Alert(
        code="exception_case",
        severity=WARNING,
        summary=f"Invoice {invoice_number} is held: " + " and ".join(parts),
        detail={
            "invoice_id": case["invoice_id"],
            "invoice_number": invoice_number,
            "items": [{"check_code": item["check_code"], "source_id": item["source_id"]} for item in items],
        },
        key=f"exception_case:{case['invoice_id']}",
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
