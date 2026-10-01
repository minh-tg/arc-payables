"""What needs a person, in one place.

The system records states, alerts and audit entries in several places, and an operator should not have
to know where to look. This collects the work that is waiting on a human, and the alerts a webhook
would have sent, into one answer.

It reads the same snapshot the metrics are rendered from, so a screen driven by this cannot disagree
with a scraper. It is read-only. Nothing here resolves anything: acting on an item still goes through
the same approval or reconciliation path a person would use by hand.
"""

from __future__ import annotations

from typing import Any

from .domain import WorkflowState, units_to_usdc

CRITICAL = "critical"
WARNING = "warning"
INFO = "info"

MAX_ITEMS = 25
"""One screen, not a dump. The count is always reported even when the list is capped."""

#: States that are waiting on a person rather than on the machine.
HUMAN_STATES: dict[str, tuple[str, str]] = {
    WorkflowState.ESCALATED.value: (WARNING, "a policy check needs a human decision"),
    WorkflowState.HELD.value: (WARNING, "held until a human links it or supplies evidence"),
    WorkflowState.NEEDS_RECONCILIATION.value: (CRITICAL, "an uncertain settlement needs a person to resolve it"),
    WorkflowState.FAILED.value: (WARNING, "a payment attempt failed"),
}


def _item(code: str, severity: str, summary: str, count: int, **detail: Any) -> dict[str, Any]:
    return {"code": code, "severity": severity, "summary": summary, "count": count, "detail": detail}


def build_attention(workflow: Any) -> dict[str, Any]:
    """Everything waiting on a person, plus the alerts the last pass raised."""
    from .metrics import collect

    snapshot = collect(workflow)
    store = workflow.store
    items: list[dict[str, Any]] = []

    by_state: dict[str, list[dict[str, Any]]] = {}
    for invoice, state in store.list_invoices():
        if state not in HUMAN_STATES:
            continue
        by_state.setdefault(state, []).append(
            {
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "supplier_id": invoice.supplier_id,
                "amount_usdc": units_to_usdc(invoice.amount_units),
                "due_date": invoice.due_date.isoformat(),
            }
        )

    for state, (severity, why) in HUMAN_STATES.items():
        waiting = by_state.get(state, [])
        if not waiting:
            continue
        items.append(
            _item(
                f"invoice_{state.lower()}",
                severity,
                f"{len(waiting)} invoice(s): {why}",
                len(waiting),
                invoices=waiting[:MAX_ITEMS],
                total_usdc=str(sum(int(item["amount_usdc"]) for item in waiting)),
            )
        )

    # Money that moved or may have moved, and the ledger's view of it.
    uncertain: list[dict[str, Any]] = []
    unrecorded: list[dict[str, Any]] = []
    for invoice, _state in store.list_invoices():
        payment = store.get_payment(invoice.id)
        if not payment:
            continue
        record = {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "transaction_hash": payment.get("transaction_hash"),
            "erp_status": payment.get("erp_status"),
            "erp_attempts": payment.get("erp_attempts"),
            "erp_next_attempt_at": payment.get("erp_next_attempt_at"),
            "erp_error_code": payment.get("erp_error_code") or payment.get("erp_fee_error_code"),
        }
        if str(payment.get("confirmation_status") or "") in {"UNCERTAIN", "PENDING"}:
            uncertain.append(record)
        if payment.get("confirmation_status") == "CONFIRMED" and str(payment.get("erp_status") or "") in {"PENDING", "UNKNOWN"}:
            unrecorded.append(record)

    if uncertain:
        items.append(
            _item(
                "settlements_unconfirmed",
                CRITICAL,
                f"{len(uncertain)} settlement(s) are not confirmed. Reconcile them, never resend.",
                len(uncertain),
                payments=uncertain[:MAX_ITEMS],
            )
        )
    if unrecorded:
        items.append(
            _item(
                "payments_not_in_ledger",
                CRITICAL,
                f"{len(unrecorded)} confirmed payment(s) are not in the ledger.",
                len(unrecorded),
                payments=unrecorded[:MAX_ITEMS],
            )
        )
    if snapshot["audit_chain_ok"] == 0:
        items.append(
            _item(
                "audit_chain_broken",
                CRITICAL,
                "the audit chain no longer verifies, so the record of what happened cannot be trusted.",
                1,
                entries=snapshot["audit_entries"],
            )
        )
    headroom = snapshot["reserve_headroom_usdc"]
    if headroom is not None and headroom < 0:
        items.append(
            _item(
                "reserve_breached",
                CRITICAL,
                f"the treasury is {abs(headroom):.6f} USDC below the configured reserve floor.",
                1,
                headroom_usdc=headroom,
                reserve_usdc=snapshot["reserve_usdc"],
            )
        )
    if not snapshot["treasury_available"]:
        items.append(
            _item("treasury_unreadable", WARNING, "the treasury balance could not be read, so coverage is unknown.", 1)
        )
    if snapshot["guard_paused"]:
        items.append(_item("guard_paused", WARNING, "the guard is paused, so no payment can settle.", 1))
    if snapshot["screenings_due"]:
        items.append(
            _item(
                "screenings_due",
                WARNING,
                f"{snapshot['screenings_due']} counterpartie(s) with open invoices are past their screening cadence.",
                snapshot["screenings_due"],
            )
        )
    if snapshot["worker_consecutive_failures"]:
        items.append(
            _item(
                "worker_failing",
                WARNING if snapshot["worker_consecutive_failures"] < 3 else CRITICAL,
                f"the worker has not finished {snapshot['worker_consecutive_failures']} pass(es) in a row.",
                snapshot["worker_consecutive_failures"],
                last_outcome=snapshot["worker_last_outcome"],
            )
        )
    if snapshot["worker_last_run_timestamp"] is None:
        items.append(_item("worker_never_ran", INFO, "no worker pass has been recorded, so nothing is being finished automatically.", 1))

    worker = store.worker_summary()
    alerts = ((worker.get("last") or {}).get("detail") or {}).get("alerts", [])

    order = {CRITICAL: 0, WARNING: 1, INFO: 2}
    items.sort(key=lambda item: (order.get(item["severity"], 3), item["code"]))
    return {
        "ok": not any(item["severity"] == CRITICAL for item in items),
        "critical": sum(1 for item in items if item["severity"] == CRITICAL),
        "warning": sum(1 for item in items if item["severity"] == WARNING),
        "items": items,
        "alerts": alerts,
        "treasury_usdc": snapshot["treasury_usdc"],
        "audit_entries": snapshot["audit_entries"],
    }
