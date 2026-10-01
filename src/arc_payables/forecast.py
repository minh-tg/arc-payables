"""Forward coverage: can the treasury actually cover what is coming?

Evaluation answers "may this be paid?", the plan answers "which first?", and this answers the
question a treasurer asks before either: *what is due, when, and where does the money run out?*

Two things are deliberately kept apart here:

* **What is owed and what the agent may pay are different questions.** An invoice whose
  evidence is incomplete is still a debt. Excluding it from the forecast would hide the
  obligation, so it is included and flagged as not payable by the agent, with the reason.
* **Timing is not priority.** The forecast walks obligations in *due-date* order and applies
  the same reserve-floor rule the payment plan uses, so the two never disagree about what is
  affordable. It shows the first date the balance can no longer cover what is due, not the
  first invoice that happens to be urgent.

Settled invoices are excluded: once money has left, the obligation is gone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from .domain import USDC_SCALE, WorkflowState, units_to_usdc
from .prioritisation import PaymentPrioritiser

#: Workflow states in which the money has already left, or is on its way out.
SETTLED_STATES = {
    WorkflowState.SUBMITTED.value,
    WorkflowState.CONFIRMED.value,
    WorkflowState.ERP_PENDING.value,
    WorkflowState.ERP_RECORDED.value,
}

#: Below a tier's limit the agent may pay unattended; at or above it a human decides.
DEFAULT_HORIZON_DAYS = 30


@dataclass(frozen=True)
class ExpectedInflow:
    external_id: str
    customer: str
    reference: str
    amount_units: int
    expected_date: date
    days_until_expected: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_id": self.external_id,
            "customer": self.customer,
            "reference": self.reference,
            "amount_usdc": units_to_usdc(self.amount_units),
            "expected_date": self.expected_date.isoformat(),
            "days_until_expected": self.days_until_expected,
        }


@dataclass(frozen=True)
class Obligation:
    invoice_id: str
    invoice_number: str
    supplier_id: str
    amount_units: int
    due_date: date
    days_until_due: int
    payable_by_agent: bool
    coverable: bool
    reasons: tuple[str, ...] = ()
    not_payable_reason: str | None = None
    discount_value_units: int = 0
    discount_deadline: date | None = None
    projected_balance_units: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "invoice_id": self.invoice_id,
            "invoice_number": self.invoice_number,
            "supplier_id": self.supplier_id,
            "amount_usdc": units_to_usdc(self.amount_units),
            "due_date": self.due_date.isoformat(),
            "days_until_due": self.days_until_due,
            "payable_by_agent": self.payable_by_agent,
            "not_payable_reason": self.not_payable_reason,
            "coverable": self.coverable,
            "reasons": list(self.reasons),
            "discount_value_usdc": units_to_usdc(self.discount_value_units) if self.discount_value_units else None,
            "discount_deadline": self.discount_deadline.isoformat() if self.discount_deadline else None,
            "projected_balance_usdc": (
                units_to_usdc(self.projected_balance_units) if self.projected_balance_units is not None else None
            ),
        }


@dataclass(frozen=True)
class Forecast:
    as_of: date
    horizon_days: int
    balance_units: int
    reserve_floor_units: int
    obligations: tuple[Obligation, ...]
    due_total_units: int
    coverable_total_units: int
    beyond_horizon_units: int
    shortfall_date: date | None
    shortfall_units: int
    uncovered: tuple[str, ...]
    rationale: str
    notes: tuple[str, ...] = field(default_factory=tuple)
    inflows: tuple[ExpectedInflow, ...] = ()
    inflow_total_units: int = 0

    @property
    def shortfall(self) -> bool:
        return self.shortfall_date is not None

    def to_dict(self) -> dict[str, Any]:
        return {
            "as_of": self.as_of.isoformat(),
            "horizon_days": self.horizon_days,
            "balance_usdc": units_to_usdc(self.balance_units),
            "reserve_floor_usdc": units_to_usdc(self.reserve_floor_units),
            "due_within_horizon_usdc": units_to_usdc(self.due_total_units),
            "coverable_usdc": units_to_usdc(self.coverable_total_units),
            "beyond_horizon_usdc": units_to_usdc(self.beyond_horizon_units),
            "shortfall": self.shortfall,
            "shortfall_date": self.shortfall_date.isoformat() if self.shortfall_date else None,
            "shortfall_usdc": units_to_usdc(self.shortfall_units) if self.shortfall else None,
            "uncovered_invoice_ids": list(self.uncovered),
            "obligations": [item.to_dict() for item in self.obligations],
            "inflows": [item.to_dict() for item in self.inflows],
            "inflow_within_horizon_usdc": units_to_usdc(self.inflow_total_units),
            "rationale": self.rationale,
            "notes": list(self.notes),
        }


def _discount_value(invoice, today: date) -> tuple[int, date | None]:
    """What paying before the discount deadline is worth, in USDC units."""
    if not invoice.discount_percent or not invoice.discount_deadline:
        return 0, None
    try:
        percent = float(invoice.discount_percent)
    except (TypeError, ValueError):
        return 0, None
    if percent <= 0 or invoice.discount_deadline < today:
        return 0, None
    value = int(round(invoice.amount_units * percent / 100))
    return value, invoice.discount_deadline


def build_forecast(workflow, *, days: int = DEFAULT_HORIZON_DAYS, today: date | None = None) -> Forecast:
    """Forward coverage over the configured horizon. Read-only: nothing is recorded."""
    today = today or date.today()
    horizon_end = today + timedelta(days=days)
    balance = workflow.payment_provider.get_balance()
    reserve_units = workflow.settings.min_reserve_units

    states = {invoice.id: state for invoice, state in workflow.store.list_invoices()}
    candidates = PaymentPrioritiser(workflow).candidates()
    payable_now = {item["invoice"].id for item in candidates if item["eligible"]}
    blocked_reason = {item["invoice"].id: item["reason"] for item in candidates if not item["eligible"]}

    obligations: list[Obligation] = []
    due_total = 0
    beyond_horizon = 0
    for item in candidates:
        invoice = item["invoice"]
        if states.get(invoice.id) in SETTLED_STATES:
            continue
        if invoice.due_date > horizon_end:
            beyond_horizon += invoice.amount_units
            continue
        discount_value, discount_deadline = _discount_value(invoice, today)
        due_total += invoice.amount_units
        reasons: list[str] = []
        if invoice.due_date < today:
            reasons.append("overdue")
        elif invoice.due_date == today:
            reasons.append("due_today")
        else:
            reasons.append(f"due_in_{(invoice.due_date - today).days}_days")
        if discount_value:
            reasons.append("early_payment_discount_expiring")
        obligations.append(
            Obligation(
                invoice_id=invoice.id,
                invoice_number=invoice.invoice_number,
                supplier_id=invoice.supplier_id,
                amount_units=invoice.amount_units,
                due_date=invoice.due_date,
                days_until_due=(invoice.due_date - today).days,
                payable_by_agent=invoice.id in payable_now,
                coverable=False,  # decided below by the same rule the plan uses
                reasons=tuple(reasons),
                not_payable_reason=None if invoice.id in payable_now else blocked_reason.get(invoice.id),
                discount_value_units=discount_value,
                discount_deadline=discount_deadline,
            )
        )

    # Due-date order, with expected inflows added back on their expected date and the same
    # reserve-floor rule the payment plan applies. Inflows never authorize anything; they only
    # move the running balance the coverage walk checks against.
    try:
        receivables = workflow.accounting.list_receivables()
    except Exception:
        receivables = []
    inflows: list[ExpectedInflow] = []
    inflow_by_date: dict[date, int] = {}
    inflow_total = 0
    for receivable in receivables:
        if receivable.expected_date > horizon_end:
            continue
        inflow_by_date[receivable.expected_date] = inflow_by_date.get(receivable.expected_date, 0) + receivable.amount_units
        inflow_total += receivable.amount_units
        inflows.append(
            ExpectedInflow(
                external_id=receivable.external_id,
                customer=receivable.customer,
                reference=receivable.reference,
                amount_units=receivable.amount_units,
                expected_date=receivable.expected_date,
                days_until_expected=(receivable.expected_date - today).days,
            )
        )
    inflows.sort(key=lambda item: (item.expected_date, item.external_id))

    obligations.sort(key=lambda item: (item.due_date, item.invoice_number))
    running = balance.balance_units
    coverable_total = 0
    shortfall_date: date | None = None
    uncovered: list[str] = []
    resolved: list[Obligation] = []
    inflow_dates = sorted(inflow_by_date)
    inflow_cursor = 0
    for obligation in obligations:
        while inflow_cursor < len(inflow_dates) and inflow_dates[inflow_cursor] <= obligation.due_date:
            running += inflow_by_date[inflow_dates[inflow_cursor]]
            inflow_cursor += 1
        covered = running - obligation.amount_units >= reserve_units
        if covered:
            running -= obligation.amount_units
            coverable_total += obligation.amount_units
        else:
            uncovered.append(obligation.invoice_id)
            if shortfall_date is None:
                shortfall_date = obligation.due_date
        resolved.append(
            Obligation(
                **{
                    **obligation.__dict__,
                    "coverable": covered,
                    "projected_balance_units": running if covered else None,
                }
            )
        )

    shortfall_units = sum(item.amount_units for item in resolved if not item.coverable)
    notes: list[str] = []
    if any(not item.payable_by_agent for item in resolved):
        notes.append(
            "Some obligations are not payable by the agent yet; they are still counted as money "
            "owed, with the reason recorded per obligation."
        )
    if beyond_horizon:
        notes.append(f"{units_to_usdc(beyond_horizon)} USDC is due beyond the {days} day horizon and is excluded.")
    if inflow_total:
        notes.append(
            f"{units_to_usdc(inflow_total)} USDC of expected inflows fall inside the horizon and are added "
            "to the running balance on their expected dates."
        )
    if not any(item.discount_value_units for item in resolved):
        notes.append("No early-payment discounts fall inside the horizon.")

    return Forecast(
        as_of=today,
        horizon_days=days,
        balance_units=balance.balance_units,
        reserve_floor_units=reserve_units,
        obligations=tuple(resolved),
        inflows=tuple(inflows),
        inflow_total_units=inflow_total,
        due_total_units=due_total,
        coverable_total_units=coverable_total,
        beyond_horizon_units=beyond_horizon,
        shortfall_date=shortfall_date,
        shortfall_units=shortfall_units,
        uncovered=tuple(uncovered),
        rationale=(
            "Obligations are walked in due-date order against the balance, keeping the configured "
            "treasury reserve intact. The first obligation the balance can no longer cover sets the "
            "shortfall date."
        ),
        notes=tuple(notes),
    )
