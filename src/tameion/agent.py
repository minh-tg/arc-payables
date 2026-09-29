from __future__ import annotations

from .domain import DecisionAction
from .ports import AgentRecommendation


class EvidenceDecisionAgent:
    """Advisory, auditable baseline. This component has no payment or signing tools.

    The agent recommends an action from recorded evidence. It cannot authorize anything: the
    deterministic policy re-derives every condition from raw evidence, and a recommendation of
    PAY_NOW is necessary but never sufficient. Any derived flag it reads (settlement support,
    accounting-source consistency) is computed once elsewhere from the raw evidence so this
    advisory view can never disagree with the authoritative gate.
    """

    def recommend(self, context: dict) -> AgentRecommendation:
        invoice = context["invoice"]
        evidence = context["accounting"]
        issues: list[str] = []

        if evidence.duplicate_invoice_id:
            issues.append("duplicate invoice reference exists")
        if evidence.invoice_status.upper() not in {"SUBMITTED", "UNPAID", "OVERDUE", "PARTLY PAID"}:
            issues.append("accounting invoice is not in a payable state")
        if not context.get("accounting_source_consistent", False):
            issues.append("invoice does not match the linked accounting source")
        if evidence.supplier is None:
            issues.append("trusted supplier record is missing")
        if not evidence.purchase_orders:
            issues.append("purchase order evidence is missing")
        if not evidence.receipts:
            issues.append("receipt evidence is missing")
        if evidence.supplier and not evidence.supplier.approved_wallet:
            issues.append("trusted supplier wallet is missing")
        if evidence.supplier and not evidence.supplier.wallet_verified:
            issues.append("supplier wallet is unverified")
        if evidence.supplier and invoice.invoice_payee_address and evidence.supplier.approved_wallet:
            if invoice.invoice_payee_address.lower() != evidence.supplier.approved_wallet.lower():
                issues.append("invoice payee differs from supplier's trusted wallet")
        if context["screening"].status.value != "CLEAR":
            issues.append("address screening is not clear")
        if not context.get("settlement_supported", False):
            issues.append("invoice currency has no configured settlement conversion")

        if issues:
            return AgentRecommendation(
                DecisionAction.ESCALATE.value,
                "Human review is needed because one or more material checks are unresolved.",
                tuple(issues),
            )
        if context["over_limit"] or context["reserve_breach"]:
            return AgentRecommendation(
                DecisionAction.ESCALATE.value,
                "The amount or post-payment treasury reserve exceeds automatic policy bounds.",
                ("configured payment limit", "treasury balance and reserve"),
            )
        if context["invoice"].due_date > context["today"] + context["due_window"] and not context["discount_due"]:
            return AgentRecommendation(
                DecisionAction.WAIT.value,
                "The invoice is not due within the payment window and no eligible early-payment discount is expiring.",
                ("invoice due date", "payment terms", "policy payment window"),
            )
        return AgentRecommendation(
            DecisionAction.PAY_NOW.value,
            "Supplier, invoice, order, receipt, payee, screening, due-date and treasury evidence support payment now.",
            ("trusted supplier record", "invoice total", "purchase order", "receipt", "treasury balance", "payment terms"),
        )
