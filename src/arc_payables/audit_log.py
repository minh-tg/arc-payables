"""The settlement log, and the audit report for one payment.

Attention answers "what needs me". This answers the question that comes afterwards, and often months
later: what happened to this payment, was it successful, and if not, why.

The list is the agent's own record of everything it tried to move. The report joins one payment's
decision, its authorization, its submission, its settlement and its ledger entry into a single
answer, and it can be reached by invoice id, invoice number, payment id or transaction hash, because
the thing an operator usually has in their hand is a hash from a block explorer.

The raw permit signature is deliberately absent. It is not needed to read the history, and returning
reusable authorization material from a status endpoint is how a read-only screen becomes a lever. The
report states whether that signature verifies against the configured signer instead.

Reading is offline. ``verify`` is the separate, explicit re-read of the chain, because reaching the
world is a different act from reading our own record.
"""

from __future__ import annotations

from typing import Any

from .domain import ARC_TESTNET_EXPLORER, units_to_usdc

MAX_ROWS = 200

SETTLED = "settled_and_recorded"
SETTLED_NOT_RECORDED = "settled_not_recorded"
IN_FLIGHT = "in_flight"
UNCONFIRMED = "unconfirmed"
FAILED = "failed"
REJECTED = "rejected"
NOT_ATTEMPTED = "not_attempted"


def _outcome(payment: dict | None, state: str | None) -> dict[str, Any]:
    """One plain answer, and the reason behind it, from the payment's own fields."""
    if not payment:
        return {
            "code": NOT_ATTEMPTED,
            "success": False,
            "summary": "No payment has been authorized for this invoice.",
            "reason": None,
        }
    confirmation = str(payment.get("confirmation_status") or "NONE")
    erp = str(payment.get("erp_status") or "NONE")
    failure = payment.get("failure_code") or payment.get("erp_error_code") or payment.get("erp_fee_error_code")

    if confirmation == "CONFIRMED" and erp == "RECORDED":
        return {
            "code": SETTLED,
            "success": True,
            "summary": "Settled on chain and recorded in the ledger.",
            "reason": None,
        }
    if confirmation == "CONFIRMED":
        return {
            "code": SETTLED_NOT_RECORDED,
            "success": False,
            "summary": f"The money moved. The ledger has not taken it (status {erp}).",
            "reason": failure,
        }
    if confirmation.startswith(("APPROVE", "GUARD", "SUBMIT")) or confirmation in {"PENDING", "NOT_SUBMITTED"}:
        return {
            "code": IN_FLIGHT,
            "success": False,
            "summary": f"Submitted and waiting. Last known status {confirmation}.",
            "reason": failure,
        }
    if confirmation in {"UNCERTAIN", "UNAVAILABLE"}:
        return {
            "code": UNCONFIRMED,
            "success": False,
            "summary": "The result is unknown. Reconcile this against the chain, never resend it.",
            "reason": failure,
        }
    if confirmation == "EXPIRED_NO_RETRY":
        return {
            "code": REJECTED,
            "success": False,
            "summary": "The permit expired before it settled. A person has to decide what happens next.",
            "reason": failure,
        }
    return {
        "code": FAILED,
        "success": False,
        "summary": f"The payment did not succeed (status {confirmation}).",
        "reason": failure,
    }


def _row(invoice: Any, state: str, payment: dict | None) -> dict[str, Any]:
    permit = (payment or {}).get("permit") or {}
    return {
        "invoice_id": invoice.id,
        "invoice_number": invoice.invoice_number,
        "supplier_id": invoice.supplier_id,
        "amount_units": invoice.amount_units,
        "amount_usdc": units_to_usdc(invoice.amount_units),
        "currency": invoice.currency,
        "recipient": permit.get("recipient"),
        "payer": permit.get("payer"),
        "state": state,
        "payment_id": (payment or {}).get("payment_id"),
        "confirmation_status": (payment or {}).get("confirmation_status") or "NOT_ATTEMPTED",
        "erp_status": (payment or {}).get("erp_status"),
        "transaction_hash": (payment or {}).get("transaction_hash"),
        "explorer_url": (
            f"{ARC_TESTNET_EXPLORER}/tx/{(payment or {}).get('transaction_hash')}"
            if (payment or {}).get("transaction_hash")
            else None
        ),
        "fee_units": (payment or {}).get("fee_units"),
        "fee_usdc": units_to_usdc((payment or {}).get("fee_units")) if (payment or {}).get("fee_units") else None,
        "erp_entry_id": (payment or {}).get("erp_entry_id"),
        "erp_fee_entry_id": (payment or {}).get("erp_fee_entry_id"),
        "settled_at": (payment or {}).get("settled_at"),
        "outcome": _outcome(payment, state),
    }


def list_payments(
    workflow: Any,
    *,
    confirmation: str | None = None,
    ledger: str | None = None,
    limit: int = MAX_ROWS,
) -> dict[str, Any]:
    """Every payment the agent has authorized, newest first, with what became of it."""
    rows: list[dict[str, Any]] = []
    for invoice, state in workflow.store.list_invoices():
        payment = workflow.store.get_payment(invoice.id)
        if payment is None:
            continue
        row = _row(invoice, state, payment)
        if confirmation and str(row["confirmation_status"]).upper() != confirmation.upper():
            continue
        if ledger and str(row["erp_status"] or "").upper() != ledger.upper():
            continue
        rows.append(row)

    rows.sort(key=lambda row: (row["settled_at"] or "", row["invoice_number"]), reverse=True)
    # Totals are summed in whole units and converted once. Summing the rendered strings is how a
    # total silently works for 250 USDC and raises for 0.01 of one.
    return {
        "count": len(rows),
        "shown": min(len(rows), limit),
        "totals": {
            "settled": sum(1 for row in rows if row["outcome"]["code"] == SETTLED),
            "settled_not_recorded": sum(1 for row in rows if row["outcome"]["code"] == SETTLED_NOT_RECORDED),
            "unconfirmed": sum(1 for row in rows if row["outcome"]["code"] == UNCONFIRMED),
            "failed": sum(1 for row in rows if row["outcome"]["code"] in {FAILED, REJECTED}),
            "value_usdc": units_to_usdc(sum(int(row["amount_units"]) for row in rows)),
            "fees_usdc": units_to_usdc(sum(int(row["fee_units"] or 0) for row in rows)),
        },
        "payments": rows[:limit],
    }


def _match(workflow: Any, reference: str) -> tuple[Any, str, dict | None, str]:
    """Find one payment by invoice id, transaction hash, payment id or invoice number.

    Order matters only for what the report says it matched on, so the most specific identifier an
    operator is likely to be holding comes first.
    """
    wanted = reference.strip()
    lowered = wanted.lower()
    invoices = workflow.store.list_invoices()
    with_payments = [(invoice, state, workflow.store.get_payment(invoice.id)) for invoice, state in invoices]
    for matcher, kind in (
        (lambda invoice, payment: invoice.id == wanted, "invoice_id"),
        (lambda invoice, payment: str((payment or {}).get("transaction_hash") or "").lower() == lowered and bool(lowered), "transaction_hash"),
        (lambda invoice, payment: str((payment or {}).get("payment_id") or "") == wanted, "payment_id"),
        (lambda invoice, payment: invoice.invoice_number == wanted, "invoice_number"),
    ):
        for invoice, state, payment in with_payments:
            if matcher(invoice, payment):
                return invoice, state, payment, kind
    raise LookupError(f"no payment matches {reference!r} by invoice id, transaction hash, payment id or invoice number")


def payment_report(workflow: Any, reference: str) -> dict[str, Any]:
    """Everything recorded about one payment, in the order a reviewer asks for it."""
    invoice, state, payment, matched_by = _match(workflow, reference)
    detail = workflow.get_invoice(invoice.id)
    events = workflow.store.events(invoice.id)
    audit = workflow.store.verify_audit_chain()

    authorization: dict[str, Any] = {"authorized": False, "signature_verified": None}
    if payment:
        permit = payment.get("permit") or {}
        signature_ok = None
        if permit and payment.get("signature"):
            try:
                from .domain import PaymentPermit

                signature_ok = bool(workflow.signer.verify(PaymentPermit(**permit), payment["signature"]))
            except Exception:
                signature_ok = None
        authorization = {
            "authorized": True,
            "permit_id": permit.get("payment_id"),
            "payer": permit.get("payer"),
            "recipient": permit.get("recipient"),
            "amount_usdc": units_to_usdc(int(permit.get("amount_units") or 0)),
            "evidence_hash": permit.get("evidence_hash"),
            "expires_at": permit.get("expiry"),
            "guard_address": permit.get("guard_address"),
            "signature_verified": signature_ok,
            "signature_note": (
                "verified against the signer this deployment is configured with"
                if signature_ok
                else "the permit does not verify against the configured signer: either that key is not "
                "the one that signed it, or the record was altered. A database read without the policy "
                "key also reports false here."
            ),
            "signer_address": getattr(workflow.signer, "address", None),
        }

    fee_units = (payment or {}).get("fee_units")
    ledger = {
        "status": (payment or {}).get("erp_status"),
        "fee_status": (payment or {}).get("erp_fee_status"),
        "payment_entry": (payment or {}).get("erp_entry_id"),
        "fee_entry": (payment or {}).get("erp_fee_entry_id"),
        "attempts": (payment or {}).get("erp_attempts"),
        "next_attempt_at": (payment or {}).get("erp_next_attempt_at"),
        "error_code": (payment or {}).get("erp_error_code") or (payment or {}).get("erp_fee_error_code"),
        "disabled_reason": (payment or {}).get("erp_error_code"),
    }

    return {
        "reference": {"matched_by": matched_by, "value": reference},
        "outcome": _outcome(payment, state),
        "invoice": detail["invoice"],
        "state": state,
        "settlement": {
            "amount_usdc": detail["invoice"]["amount"],
            "currency": detail["invoice"]["currency"],
            "recipient": authorization.get("recipient"),
            "payer": authorization.get("payer"),
            "transaction_hash": (payment or {}).get("transaction_hash"),
            "explorer_url": (
                f"{ARC_TESTNET_EXPLORER}/tx/{(payment or {}).get('transaction_hash')}"
                if (payment or {}).get("transaction_hash")
                else None
            ),
            "confirmation_status": (payment or {}).get("confirmation_status"),
            "provider": (payment or {}).get("provider_stage"),
            "provider_transaction_id": (payment or {}).get("provider_transaction_id"),
            "fee_units": fee_units,
            "fee_usdc": units_to_usdc(fee_units) if fee_units else None,
            "settled_at": (payment or {}).get("settled_at"),
            "failure_code": (payment or {}).get("failure_code"),
        },
        "authorization": authorization,
        "ledger": ledger,
        "decision": detail["decision"],
        "evidence": detail["evidence"],
        "approvals": detail["approvals"],
        "timeline": [
            {
                "when": event.get("created_at"),
                "event": event["type"],
                "state": event.get("state"),
                "entry_hash": event.get("event_hash"),
                "signed": bool(event.get("signature")),
                "explanation": (event.get("payload") or {}).get("reason")
                or (event.get("payload") or {}).get("error_code"),
            }
            for event in events
        ],
        "audit": {
            "ok": bool(audit.get("ok")),
            "entries": audit.get("length"),
            "reason": audit.get("reason"),
        },
        "next_actions": detail["next_actions"],
    }


def verify_payment(workflow: Any, reference: str) -> dict[str, Any]:
    """Ask the provider and the chain again about one settlement. Reads only; nothing is sent.

    This is the independent check: the report above is our own record, and this is what the world
    says now. It also compares the fee we booked against the fee the chain reports today, because a
    fee that was unreadable at settlement time is exactly how a payment ends up unrecorded.
    """
    invoice, state, payment, matched_by = _match(workflow, reference)
    if payment is None:
        return {
            "reference": {"matched_by": matched_by, "value": reference},
            "checked": False,
            "detail": "no payment has been authorized for this invoice, so there is nothing to check",
        }
    try:
        submission = workflow.payment_provider.inspect_payment(payment)
    except Exception as exc:
        return {
            "reference": {"matched_by": matched_by, "value": reference},
            "checked": False,
            "detail": f"{type(exc).__name__}: {exc}"[:200],
        }

    recorded_fee = payment.get("fee_units")
    reported_fee = submission.fee_units
    # Compare like with like. fee_units is the total cost of *every* on-chain operation, while a
    # provider answering an inspection names the one operation it was asked about. Comparing the two
    # directly marked every multi-operation payment as a disagreement, which is a false alarm on the
    # one screen that exists to confirm the books independently - and an invitation to "correct" the
    # ledger down to the smaller figure, losing the allowance fee. The settlement stage is taken from
    # the record when it has one; a record written before per-stage capture holds that figure alone.
    # Provider stage strings are provider-specific (the mock reports "contract_execution"), so they
    # are mapped onto the stage names used inside fee_breakdown. An inspection resolves the
    # settlement operation, which is the default when the recorded stage names nothing we track.
    stage = str(payment.get("provider_stage") or "guard")
    if stage not in ("approve", "approve_reset", "guard"):
        stage = "guard"
    breakdown = payment.get("fee_breakdown") or {}
    if breakdown:
        recorded_stage_fee = breakdown.get(stage)
    else:
        recorded_stage_fee = recorded_fee
    # A record whose total disagrees with its own stages was edited inconsistently. That is worth
    # reporting on its own rather than letting it decide whether the provider's figure is correct.
    if breakdown and recorded_fee is not None:
        fee_record_is_consistent: bool | None = int(recorded_fee) == sum(int(value) for value in breakdown.values())
    else:
        fee_record_is_consistent = None
    return {
        "reference": {"matched_by": matched_by, "value": reference},
        "checked": True,
        "provider_status": submission.status.value,
        "transaction_hash": submission.transaction_hash or payment.get("transaction_hash"),
        "failure_code": submission.failure_code,
        "fee_units_recorded": recorded_fee,
        "fee_units_reported_now": reported_fee,
        "fee_stage_compared": stage,
        "fee_units_recorded_for_stage": recorded_stage_fee,
        "fee_record_is_consistent": fee_record_is_consistent,
        "fee_agrees": (
            None
            if (recorded_stage_fee is None or reported_fee is None)
            else int(recorded_stage_fee) == int(reported_fee)
        ),
        "recorded_confirmation_status": payment.get("confirmation_status"),
        "agrees_with_record": submission.status.value.lower() == str(payment.get("confirmation_status") or "").lower(),
        "detail": (
            "the provider still reports this settlement as confirmed"
            if submission.status.value == "CONFIRMED"
            else f"the provider reports {submission.status.value}"
        ),
    }
