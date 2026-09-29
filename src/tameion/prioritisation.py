"""Which payable to pay first, when the treasury cannot cover them all.

Individual evaluation answers "may this invoice be paid?". It cannot answer the question a
treasury actually faces: several invoices are payable, the balance covers some of them, and
paying the wrong one first costs money or breaks a promise. This module answers that second
question without creating a new way to authorize anything.

The split is deliberate and load-bearing:

* **Ordering is advisory.** It ranks candidates and explains itself. Every ranked payment must
  still pass the same per-invoice evaluation, policy, permit and on-chain guard.
* **Spending is deterministic.** The allocation applies the reserve floor and the balance in
  code, by walking the order and stopping when the next invoice would breach the reserve. A
  deliberating layer may reorder the queue; it never decides how much money leaves.

So the worst a confused or compromised planner can do is sequence the same payments
differently. It cannot add an invoice, drop the floor, or spend a cent more.

Building a plan is read-only: it derives decisions from live evidence without recording them,
so asking "what should we pay next?" never mutates workflow state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .agent import EvidenceDecisionAgent
from .domain import DecisionAction, InvoiceRecord, USDC_SCALE, units_to_usdc
from .service import WorkflowError

#: Codes recorded for invoices that are visible in a plan but not payable.
EXCLUDED_NOT_ELIGIBLE = "not_payable_now"
EXCLUDED_BELOW_RESERVE = "would_breach_the_reserve_floor"
EXCLUDED_EVIDENCE_UNAVAILABLE = "evidence_unavailable"

DISCOUNT_RANK_WINDOW_DAYS = 3
DUE_IMMINENT_DAYS = 1


def rank_key(invoice: InvoiceRecord, today: date) -> tuple:
    """The fast, explainable ordering. Highest sorts first.

    A tuple of business facts rather than weighted magic numbers, so a reviewer can read the
    reason for a position straight off the invoice: an expiring discount beats a late invoice,
    lateness beats imminentness, and between equals the smaller obligation goes first, because
    it clears more of the queue per unit of cash.
    """
    discount = 1 if _discount_expiring(invoice, today) else 0
    overdue = 1 if invoice.due_date < today else 0
    due_soon = 1 if invoice.due_date <= today + timedelta(days=DUE_IMMINENT_DAYS) else 0
    return (discount, overdue, due_soon, -invoice.amount_units, invoice.due_date.isoformat(), invoice.invoice_number)


def rank_reasons(invoice: InvoiceRecord, today: date) -> list[str]:
    """Named reasons for a position, ordered by the sort key they correspond to."""
    reasons: list[str] = []
    if _discount_expiring(invoice, today):
        reasons.append("early_payment_discount_expiring")
    if invoice.due_date < today:
        reasons.append("already_overdue")
    elif invoice.due_date <= today + timedelta(days=DUE_IMMINENT_DAYS):
        reasons.append("due_immediately")
    if invoice.discount_percent:
        reasons.append(f"discount_{invoice.discount_percent}_percent_available")
    return reasons or ["no_urgency_signal"]


def _discount_expiring(invoice: InvoiceRecord, today: date) -> bool:
    return bool(
        invoice.discount_percent
        and invoice.discount_deadline
        and today <= invoice.discount_deadline <= today + timedelta(days=DISCOUNT_RANK_WINDOW_DAYS)
    )


@dataclass(frozen=True)
class PlanEntry:
    invoice_id: str
    invoice_number: str
    supplier_id: str
    amount_usdc: str
    action: str
    reason: str
    reasons: tuple[str, ...] = ()
    projected_balance_usdc: str | None = None
    decision_action: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_number": self.invoice_number,
            "supplier_id": self.supplier_id,
            "amount_usdc": self.amount_usdc,
            "action": self.action,
            "reason": self.reason,
            "reasons": list(self.reasons),
            "projected_balance_usdc": self.projected_balance_usdc,
            "decision_action": self.decision_action,
        }


@dataclass(frozen=True)
class PaymentPlan:
    ordered: tuple[PlanEntry, ...]
    excluded: tuple[PlanEntry, ...]
    balance_usdc: str
    reserve_floor_usdc: str
    spendable_usdc: str
    planned_spend_usdc: str
    ordered_by: str
    rationale: str
    deliberations: tuple[dict, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordered": [entry.to_dict() for entry in self.ordered],
            "excluded": [entry.to_dict() for entry in self.excluded],
            "balance_usdc": self.balance_usdc,
            "reserve_floor_usdc": self.reserve_floor_usdc,
            "spendable_usdc": self.spendable_usdc,
            "planned_spend_usdc": self.planned_spend_usdc,
            "ordered_by": self.ordered_by,
            "rationale": self.rationale,
            "deliberations": [dict(item) for item in self.deliberations],
        }


def allocate(ranked: list[dict], balance_units: int, reserve_units: int) -> tuple[list[PlanEntry], list[PlanEntry], int]:
    """Walk an order and spend in code, stopping at the reserve floor.

    Deterministic on purpose: the ordering may come from a model, what is affordable never
    does. An invoice the balance cannot cover is reported with its reason rather than silently
    dropped, so the whole queue stays visible in the plan.
    """
    ordered: list[PlanEntry] = []
    excluded: list[PlanEntry] = []
    remaining = balance_units
    spent = 0
    for candidate in ranked:
        invoice: InvoiceRecord = candidate["invoice"]
        entry = {
            "invoice_id": invoice.id,
            "invoice_number": invoice.invoice_number,
            "supplier_id": invoice.supplier_id,
            "amount_usdc": units_to_usdc(invoice.amount_units),
            "reasons": tuple(candidate["reasons"]),
            "decision_action": candidate["decision_action"],
        }
        if remaining - invoice.amount_units < reserve_units:
            # The reserve code is added to the entry's own reasons rather than replacing them,
            # so the plan still shows why the invoice was a candidate in the first place.
            unaffordable = {**entry, "reasons": tuple(entry["reasons"]) + (EXCLUDED_BELOW_RESERVE,)}
            excluded.append(PlanEntry(
                **unaffordable,
                action=DecisionAction.WAIT.value,
                reason=(
                    "Paying this now would leave less than the configured treasury reserve of "
                    f"{units_to_usdc(reserve_units)} USDC."
                ),
                projected_balance_usdc=None,
            ))
            continue
        remaining -= invoice.amount_units
        spent += invoice.amount_units
        ordered.append(PlanEntry(
            **entry,
            action=DecisionAction.PAY_NOW.value,
            reason=candidate["reason"],
            projected_balance_usdc=units_to_usdc(remaining),
        ))
    return ordered, excluded, spent


class PaymentPrioritiser:
    """Builds a plan from live evidence. Read-only: it records and authorizes nothing."""

    name = "heuristics"

    def __init__(self, workflow, planner=None):
        self.workflow = workflow
        self.planner = planner
        # Ordering always starts from the fast, explainable layer, even when a planner is
        # configured, so there is a trustworthy order to fall back to.
        self.fast = EvidenceDecisionAgent()

    def candidates(self, approvals: dict[str, dict] | None = None) -> list[dict]:
        """Every invoice with a live, read-only decision, ranked by the fast layer."""
        today = date.today()
        approvals = approvals or {}
        eligible: list[dict] = []
        blocked: list[dict] = []
        for invoice, _state in self.workflow.store.list_invoices():
            try:
                context = self.workflow._load_context(invoice)
            except WorkflowError as exc:
                blocked.append(_blocked(invoice, EXCLUDED_EVIDENCE_UNAVAILABLE, str(exc)))
                continue
            recommendation = self.fast.recommend(self.workflow._agent_context(invoice, context))
            decision = self.workflow._decide(invoice, context, recommendation, approvals.get(invoice.id))
            if decision.action != DecisionAction.PAY_NOW:
                blocked.append(_blocked(invoice, decision.action.value, decision.reason, decision.action.value))
                continue
            eligible.append({
                "invoice": invoice,
                "decision_action": decision.action.value,
                "reasons": rank_reasons(invoice, today),
                "reason": "Payable now and ranked highest among the invoices the balance can cover.",
                "eligible": True,
                "sort_key": rank_key(invoice, today),
            })
        eligible.sort(key=lambda item: item["sort_key"], reverse=True)
        return eligible + blocked

    def plan(self, approvals: dict[str, dict] | None = None) -> PaymentPlan:
        balance = self.workflow.payment_provider.get_balance()
        reserve_units = self.workflow.settings.min_reserve_units
        ranked = self.candidates(approvals)
        payable = [item for item in ranked if item["eligible"]]
        blocked = [item for item in ranked if not item["eligible"]]

        order, deliberations = self._order(payable, balance.balance_units, reserve_units)
        ordered, unaffordable, spent = allocate(order, balance.balance_units, reserve_units)

        excluded = [
            PlanEntry(
                invoice_id=item["invoice"].id,
                invoice_number=item["invoice"].invoice_number,
                supplier_id=item["invoice"].supplier_id,
                amount_usdc=units_to_usdc(item["invoice"].amount_units),
                action=DecisionAction.HOLD.value,
                reason=item["reason"],
                reasons=(item["code"], item["detail_code"]),
                decision_action=item["decision_action"],
            )
            for item in blocked
        ] + unaffordable

        return PaymentPlan(
            ordered=tuple(ordered),
            excluded=tuple(excluded),
            balance_usdc=units_to_usdc(balance.balance_units),
            reserve_floor_usdc=units_to_usdc(reserve_units),
            spendable_usdc=units_to_usdc(max(0, balance.balance_units - reserve_units)),
            planned_spend_usdc=units_to_usdc(spent),
            ordered_by="planner" if deliberations and deliberations[0].get("outcome") == "used" else self.name,
            rationale=(
                "Ordered by expiring discount, then lateness, then imminent due dates, then the "
                "smaller obligation. Spending stops at the configured treasury reserve, and every "
                "ranked invoice must still pass its own policy checks before it can be paid."
            ),
            deliberations=tuple(deliberations),
        )

    def _order(self, payable: list[dict], balance_units: int, reserve_units: int) -> tuple[list[dict], list[dict]]:
        """Let a planner reorder the queue when it answers usefully; otherwise keep the fast order."""
        if self.planner is None or len(payable) < 2:
            return payable, []
        summary = [
            {
                "invoice_id": item["invoice"].id,
                "amount_usdc": units_to_usdc(item["invoice"].amount_units),
                "due_date": item["invoice"].due_date.isoformat(),
                "discount_percent": item["invoice"].discount_percent,
                "discount_deadline": item["invoice"].discount_deadline.isoformat() if item["invoice"].discount_deadline else None,
                "fast_reasons": list(item["reasons"]),
            }
            for item in payable
        ]
        order_ids, trace = self.planner.order_payables(summary, balance_units, reserve_units)
        if order_ids is None:
            return payable, [trace]
        by_id = {item["invoice"].id: item for item in payable}
        reordered = [dict(by_id[invoice_id]) for invoice_id, _reason in order_ids]
        for position, (invoice_id, reason) in enumerate(order_ids):
            reordered[position]["reason"] = reason
        return reordered, [trace]


def _blocked(invoice: InvoiceRecord, detail_code: str, reason: str, decision_action: str | None = None) -> dict:
    """An invoice present in the plan but not payable, with why.

    ``code`` is the plan-level status and ``detail_code`` records the specific cause (the
    policy's action, or unavailable evidence), so a reader sees both.
    """
    return {
        "invoice": invoice,
        "decision_action": decision_action,
        "reason": reason,
        "code": EXCLUDED_NOT_ELIGIBLE,
        "detail_code": detail_code,
        "eligible": False,
        "sort_key": None,
    }


def plan_digest(plan: PaymentPlan) -> str:
    """Stable digest of a plan, so a reviewer can tell whether the advice changed."""
    return hashlib.sha256(json.dumps(plan.to_dict(), sort_keys=True).encode()).hexdigest()


__all__ = [
    "EXCLUDED_BELOW_RESERVE",
    "EXCLUDED_EVIDENCE_UNAVAILABLE",
    "EXCLUDED_NOT_ELIGIBLE",
    "PaymentPlan",
    "PaymentPrioritiser",
    "PlanEntry",
    "allocate",
    "plan_digest",
    "rank_key",
    "rank_reasons",
    "USDC_SCALE",
]
