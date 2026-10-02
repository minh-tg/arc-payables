"""Plain words for everything the system says, so no one has to decode it.

The policy writes machine-readable reasons. Reviews, audits and hackathon judges are people.
This module is the mapping between the two: one short line of plain language for every workflow
state, decision, check, attention item, payment outcome, guard rejection, and setup requirement,
plus what to do next. The codes stay where they are for machines. The words live here.

Nothing here changes a decision. A wrong explanation here misleads exactly as a wrong sentence in
the README does, so keep each line short, concrete, and in the same plain style as everything else.
"""

from __future__ import annotations

from typing import Any


def _row(plain: str, action: str | None = None) -> dict[str, Any]:
    return {"plain": plain, "action": action}


#: Every workflow state an invoice can be in.
STATES: dict[str, dict[str, Any]] = {
    "RECEIVED": _row("Just captured. Nothing has been checked yet."),
    "EVIDENCE_CHECKING": _row("The agent is checking this against the accounting books right now."),
    "ELIGIBLE": _row("Cleared for payment. Nothing is blocking it.", "Pay it, or let the worker pay it."),
    "WAITING": _row("Not due yet, or only worth paying later. Waiting is a decision, not an oversight."),
    "HELD": _row(
        "Paused by a human. The agent will not touch it until someone releases it.",
        "A person with the approval token can release it or keep it held.",
    ),
    "ESCALATED": _row(
        "Stopped for a human. One or more checks need a person to clear them.",
        "Review the failing checks below, acknowledge the ones you accept, then approve.",
    ),
    "AUTHORIZED": _row("The permit is signed and the payment is authorised, but not yet sent."),
    "SUBMITTED": _row("Sent to the payment provider and waiting for the chain to confirm it."),
    "CONFIRMED": _row("The chain accepted it. The money has moved."),
    "ERP_PENDING": _row("Settled, but the accounting books have not accepted it yet."),
    "ERP_RECORDED": _row("Done. Paid on chain and booked in the ledger."),
    "FAILED": _row(
        "The payment failed and the agent stopped. It will not retry on its own.",
        "Read the failure code, fix the cause, and act.",
    ),
    "NEEDS_RECONCILIATION": _row(
        "The result is uncertain, so the agent refuses to assume anything about the money.",
        "Ask the chain what really happened, then write back the result by hand.",
    ),
}

#: Every decision the policy can reach.
DECISIONS: dict[str, dict[str, Any]] = {
    "PAY_NOW": _row("Pay this. Every check passed.", "Pay it, or let the autopay worker pay it."),
    "WAIT": _row("Do nothing yet. It is not due, or paying early is not worth it."),
    "HOLD": _row(
        "Keep it held. The agent must not act on it.",
        "A person with the approval token decides when to release it.",
    ),
    "ESCALATE": _row(
        "Needs a human before anything else happens.",
        "Review the failed checks below and acknowledge the ones you accept.",
    ),
}

#: Every policy check, passing or extended when it fails.
CHECKS: dict[str, dict[str, Any]] = {
    "invoice_payable": _row("The accounting books show this invoice as owed, not paid or cancelled."),
    "duplicate_invoice": _row("This invoice number appears once. A matching reference would smell like a double bill."),
    "supplier_record": _row(
        "The supplier exists in the accounting books, so the destination comes from a record you verified.",
        "If this fails: review the supplier in ERPNext, not the invoice.",
    ),
    "supplier_blocked": _row(
        "The accounting system itself blocked this supplier. A finance team already made this call.",
        "Clear the hold or disabled state in the accounting system. Nothing here can override it.",
    ),
    "invoice_line_total": _row("The line items add up to the billed total."),
    "purchase_order_match": _row("The purchase order covers what was billed, for the same supplier."),
    "missing_purchase_order": _row(
        "No purchase order supports this invoice. Without one, there is no proof anyone approved the spend.",
        "Link one, or have a human waive it explicitly.",
    ),
    "receipt_match": _row("The goods were received in the invoiced quantities."),
    "missing_receipt": _row(
        "No goods receipt covers these lines. Nothing has proven this arrived.",
        "Record the receipt, or have a human waive it explicitly.",
    ),
    "settlement_currency": _row(
        "The invoice is payable in USDC, so no exchange rate has to be guessed.",
        "A foreign currency needs explicit rate configuration first.",
    ),
    "payee_mismatch": _row(
        "The address on the invoice is not where the money would go. Only the trusted supplier record decides the destination.",
        "If you still want to pay: acknowledge this check, after confirming the supplier record is right.",
    ),
    "wallet_unverified": _row(
        "Nobody has verified the supplier wallet yet. Money stops here until they do; verification means a human confirmed the address belongs to the supplier.",
        "Tick the wallet verified box in ERPNext. That is the one step the tooling never does for you.",
    ),
    "address_screening": _row(
        "The counterparty cleared sanctions and watchlist screening.",
        "If it fails: the risk tier shrinks what may be paid, or a human reviews.",
    ),
    "amount_limit": _row("The amount fits the automatic limit for this counterparty's risk tier."),
    "cash_reserve": _row("Paying leaves the configured treasury reserve untouched."),
    "treasury_fresh": _row("The treasury balance was read seconds ago, not hours ago."),
    "payment_timing": _row("Now is a reasonable time to pay: due soon, or a discount makes early payment pay."),
    "early_discount": _row("An early-payment discount is available and was priced into the timing."),
}

#: Every screening status, with what it means for payment.
SCREENING: dict[str, dict[str, Any]] = {
    "CLEAR": _row("The counterparty cleared sanctions and watchlist screening."),
    "INCONCLUSIVE": _row(
        "Screening could not decide. The counterparty is allowed less, not blocked.",
        "It may be paid up to a quarter of the automatic limit, or wait for a human.",
    ),
    "UNAVAILABLE": _row(
        "Screening never ran, so nobody can say it cleared. Treated like an unclear result, and the service reports it as screening_unavailable.",
        "Configure a screening provider, or wait for a human.",
    ),
    "FLAGGED": _row(
        "A sanctions or watchlist list names this counterparty. Payment stops here.",
        "Do not pay. Investigate against the original source list.",
    ),
}

#: Every attention item the operator console can show.
ATTENTION: dict[str, dict[str, Any]] = {
    "invoice_escalated": _row("A policy check needs a human decision.", "Open the invoice, read the failing checks, acknowledge and approve."),
    "invoice_held": _row("Held until a human links it or supplies evidence.", "Link the accounting payable, then re-evaluate."),
    "invoice_needs_reconciliation": _row(
        "An uncertain settlement needs a person to resolve it. The money may have moved.",
        "Ask the chain what really happened, then write back the result by hand. Never resend it.",
    ),
    "invoice_failed": _row("A payment attempt failed and the agent stopped.", "Read the failure code and fix the cause."),
    "settlements_unconfirmed": _row(
        "A settlement nobody confirmed. The chain has the answer; our record does not.",
        "Wait for confirmation, or reconcile. Never resend an uncertain payment.",
    ),
    "payments_not_in_ledger": _row(
        "Confirmed money that the books have not accepted. The supplier was paid; the paperwork is behind.",
        "Read the attempt count and error. Retry the writeback, which is idempotent and cannot move money twice.",
    ),
    "audit_chain_broken": _row(
        "The signed history no longer verifies. Stop: the record of what happened cannot be trusted.",
        "Stop payment operations, preserve the database, and investigate before resuming.",
    ),
    "reserve_breached": _row(
        "The treasury is below the configured reserve floor. Policy refused the payment on purpose.",
        "Fund the treasury, or lower the floor as a stated decision.",
    ),
    "treasury_unreadable": _row(
        "The treasury balance could not be read, so coverage is unknown and nothing is decided.",
        "Check the provider connection, then re-run.",
    ),
    "guard_paused": _row(
        "The payment guard is paused, so no payment can settle. Pausing moves no money in either direction.",
        "Unpause through the operator address if that was your call; otherwise find out whose it was.",
    ),
    "screenings_due": _row(
        "A counterparty with open invoices is past its screening cadence. Its tier may be stale.",
        "Run the re-screen, then read any tier changes.",
    ),
    "worker_failing": _row(
        "The background loop is not finishing its passes. An operator who trusts a quiet worker is worse off than one with none.",
        "Read the latest pass steps, fix the fault, and confirm the next pass finishes.",
    ),
    "worker_never_ran": _row(
        "No worker pass has ever been recorded, so nothing is being finished automatically.",
        "Start the worker, or run one pass by hand to see what it does.",
    ),
}

#: Every payment outcome the audit report can carry.
OUTCOMES: dict[str, dict[str, Any]] = {
    "settled_and_recorded": _row("Paid on chain and booked in the ledger. Done."),
    "settled_not_recorded": _row(
        "The money moved. The ledger has not accepted it yet, so the paperwork is behind.",
        "Read the ledger error below. Writing back is idempotent and cannot move money twice.",
    ),
    "in_flight": _row("Sent and waiting for the chain. A submitted payment is neither success nor failure."),
    "unconfirmed": _row(
        "The result is unknown, so nobody may assume anything about the money.",
        "Reconcile against the chain, never resend it.",
    ),
    "failed": _row(
        "The payment did not go through. The reason below says why, and the agent stopped rather than guessing.",
        "Fix the cause named in the reason, then act.",
    ),
    "rejected": _row(
        "The permit expired before it settled. Expired approvals do not come back.",
        "A person decides whether this is paid again.",
    ),
    "not_attempted": _row("Nothing happened yet. No payment has been authorized for this invoice."),
}

#: Every settlement confirmation status the payment row can carry.
CONFIRMATIONS: dict[str, dict[str, Any]] = {
    "CONFIRMED": _row("The chain accepted the settlement."),
    "PENDING": _row("Submitted and waiting for the chain."),
    "UNCERTAIN": _row(
        "Nobody knows the result. Treated as if the money moved, until proven otherwise.",
        "Reconcile, never resend.",
    ),
    "UNAVAILABLE": _row(
        "The provider could not be asked. Our record says nothing about the money.",
        "Try again when the provider is reachable.",
    ),
    "FAILED": _row("The settlement failed. The agent stopped and will not retry on its own."),
    "HASH_MISSING": _row(
        "Confirmed, but the hash is missing from the record, so the Explorer link cannot be built.",
        "Reconcile the provider transaction and repair the record.",
    ),
    "EXPIRED_NO_RETRY": _row(
        "The permit expired before settlement. Expired approvals do not come back.",
        "A person decides whether this is paid again.",
    ),
    "SUBMITTING": _row("Being handed to the provider right now."),
    "NOT_SUBMITTED": _row("Authorized, but not yet handed to the provider."),
}

#: Every step of a worker pass.
STEPS: dict[str, dict[str, Any]] = {
    "reconcile": _row("Asks the chain again about settlements whose confirmation never arrived."),
    "writeback": _row("Finishes confirmed payments the ledger has not accepted yet, fee entry included."),
    "intake": _row("Reads what the ledger still owes, captures what nobody imported, and evaluates it."),
    "autopay": _row("Pays only what policy already authorized. Never approves anything."),
    "rescreen": _row("Re-screens counterparties past their cadence and moves their risk tier."),
    "observe": _row("Reads the treasury balance, the reserve headroom and the guard's budgets. Writes nothing."),
}

#: Every alert a pass can raise.
ALERTS: dict[str, dict[str, Any]] = {
    "worker_failing": _row(
        "Passes in a row did not finish cleanly. One degraded pass is noise; a run of them means the loop is not doing its job."
    ),
    "audit_chain_broken": _row(
        "The audit chain no longer verifies. This outranks everything else here, because if the record cannot be trusted, nothing downstream of it can be either."
    ),
    "reserve_breached": _row("The treasury is below the configured reserve floor."),
    "settlements_unconfirmed": _row("Settlements that have not been confirmed."),
    "payments_not_in_ledger": _row("Confirmed payments that are not in the ledger."),
}

#: The frequently misunderstood consequences, in plain questions.
GUARD: dict[str, dict[str, Any]] = {
    "caps": _row("The guard enforces three hard budgets: per payment, per time window, and per recipient per window."),
    "immutable": _row(
        "The budget cannot be raised or lowered in place. A new budget is a new deployment, which keeps temporary pressure from becoming a permanent limit."
    ),
    "refund": _row(
        "Returning funds does not restore a spent allowance. The allowance counts payments made, not money currently held.",
        "When a recipient's allowance is spent, pay a different recipient or wait for the window to roll.",
    ),
    "permit": _row("A payment authorization names its payer, token, recipient, amount, evidence hash and expiry, and the guard refuses anything outside them."),
    "pauser": _row("A separate address can freeze and unfreeze payments. It cannot move, redirect or resize anything."),
}

#: Setup states, for the settings a deployment can be missing.
SETUP: dict[str, dict[str, Any]] = {
    "set": _row("Present and used."),
    "missing": _row("Not configured. Whatever the description says it breaks stays broken until this is set."),
    "demo_default": _row(
        "Still carrying the value shipped as a demo example. Worked examples are not recommendations.",
        "Set a value this business actually means before a live run.",
    ),
    "not_needed": _row("Not required with the providers currently selected. Nothing to do."),
}

#: Risk tiers, with why unclear is not the same as bad.
TIERS: dict[str, dict[str, Any]] = {
    "low": _row("Screened clear. Pays unattended up to the full automatic limit."),
    "medium": _row(
        "Unclear, not forbidden. Pays unattended at a quarter of the automatic limit when configured that way, otherwise needs a human.",
        "An unclear result buys a smaller payment, never an automatic pass.",
    ),
    "high": _row("Flagged. Pays nothing unattended and, by default, nothing even with a reviewer."),
}


def look_up(table: dict[str, dict[str, Any]], code: str) -> dict[str, Any]:
    """The plain words for one code, or a stated admission that no words were written."""
    entry = table.get(str(code or ""))
    if entry is None:
        return {"plain": f"No explanation was written for {code!r}. That is a documentation gap, not a verdict.", "action": None}
    return {"plain": entry["plain"], "action": entry.get("action")}


def check_explanation(check: dict[str, Any]) -> dict[str, Any]:
    """Plain words for one policy check result, using the code the service already returned."""
    entry = look_up(CHECKS, check.get("code", ""))
    words = dict(entry)
    if not check.get("passed", True):
        fail_line = check.get("detail") or check.get("reason") or ""
        words["plain"] = f"{entry['plain']} Right now it is failing: {fail_line}" if fail_line else entry["plain"]
    return {**check, "explanation": words}
