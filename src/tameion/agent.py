"""The fast, always-available advisory layer.

This is deliberately not a gate. It reads the same structured evidence the deterministic
policy reads and forms an opinion quickly, with reasons, so that the common case needs no
model call at all. The policy re-derives everything from raw evidence afterwards, and a
``PAY_NOW`` here is necessary but never sufficient.

Two kinds of judgement live here:

* **hard evidence gaps** - a missing order, an unverified wallet, a mismatched invoice. These
  are not opinions: no amount of reasoning should talk the system past them, and they are
  never sent for deliberation.
* **judgement calls** - amount against an automatic limit, treasury reserve, payment timing,
  early-payment discount. These are genuinely trade-offs, and they are the only things worth
  deliberating about when a slower layer is configured.

Both are recorded as named observations, so a reviewer reads *why* rather than inferring it.
"""

from __future__ import annotations

from decimal import Decimal

from .domain import DecisionAction
from .ports import AgentRecommendation

#: Observations that a slower deliberating layer may revisit, because they are trade-offs
#: between acceptable outcomes rather than missing facts. An expiring discount is deliberately
#: not here: it is an argument for paying now, not a reason to hesitate.
JUDGEMENT_CODES = {"amount_above_automatic_limit", "treasury_reserve_pressure", "not_due_yet"}


class EvidenceDecisionAgent:
    """Advisory, auditable, fast. This component has no payment or signing tools.

    It cannot authorize anything: the deterministic policy re-derives every condition from raw
    evidence, and a recommendation of PAY_NOW is necessary but never sufficient. Any derived
    flag it reads (settlement support, accounting-source consistency) is computed once
    elsewhere from the raw evidence so this advisory view can never disagree with the
    authoritative gate.
    """

    name = "heuristics"

    def recommend(self, context: dict) -> AgentRecommendation:
        observations = self.observe(context)
        blockers = [item for item in observations if item["severity"] == "blocker"]
        judgements = [item for item in observations if item["severity"] == "judgement"]
        # Codes describe the reason for this action, so a blocker vetoes deliberation entirely:
        # absent evidence is not a trade-off, and no amount of reasoning supplies it.
        codes = tuple(item["code"] for item in (blockers or judgements))

        if blockers:
            return AgentRecommendation(
                DecisionAction.ESCALATE.value,
                "Human review is needed because one or more material checks are unresolved.",
                tuple(item["claim"] for item in blockers),
                decided_by=self.name,
                rationale="; ".join(item["detail"] for item in blockers),
                confidence="high",
                evidence_used=tuple(item["claim"] for item in observations),
                fast_path_action=DecisionAction.ESCALATE.value,
                observation_codes=codes,
            )
        if judgements:
            action = DecisionAction.WAIT.value if set(codes) == {"not_due_yet"} else DecisionAction.ESCALATE.value
            return AgentRecommendation(
                action,
                "The amount, treasury reserve or payment timing needs a human decision."
                if action == DecisionAction.ESCALATE.value
                else "The invoice is not due within the payment window and no eligible early-payment discount is expiring.",
                tuple(item["claim"] for item in judgements),
                decided_by=self.name,
                rationale="; ".join(item["detail"] for item in judgements),
                confidence="medium",
                evidence_used=tuple(item["claim"] for item in observations),
                fast_path_action=action,
                observation_codes=codes,
            )
        return AgentRecommendation(
            DecisionAction.PAY_NOW.value,
            "Supplier, invoice, order, receipt, payee, screening, due-date and treasury evidence support payment now.",
            ("trusted supplier record", "invoice total", "purchase order", "receipt", "treasury balance", "payment terms"),
            decided_by=self.name,
            rationale="Every recorded evidence check passed and the invoice is due within the policy window.",
            confidence="high",
            evidence_used=tuple(item["claim"] for item in observations),
            fast_path_action=DecisionAction.PAY_NOW.value,
            observation_codes=codes,
        )

    @staticmethod
    def needs_deliberation(recommendation: AgentRecommendation) -> bool:
        """True only for trade-offs. Missing facts are never worth a slower opinion."""
        return any(code in JUDGEMENT_CODES for code in recommendation.observation_codes)

    @staticmethod
    def observe(context: dict) -> list[dict]:
        """Named observations, each with a code, severity, claim and human-readable detail."""
        invoice = context["invoice"]
        evidence = context["accounting"]
        observations: list[dict] = []

        def blocker(code: str, claim: str, detail: str) -> None:
            observations.append({"code": code, "severity": "blocker", "claim": claim, "detail": detail})

        def judgement(code: str, claim: str, detail: str) -> None:
            observations.append({"code": code, "severity": "judgement", "claim": claim, "detail": detail})

        if evidence.duplicate_invoice_id:
            blocker("duplicate_invoice", "duplicate invoice reference exists", "An accounting invoice already carries this supplier invoice reference.")
        if evidence.invoice_status.upper() not in {"SUBMITTED", "UNPAID", "OVERDUE", "PARTLY PAID"}:
            blocker("invoice_not_payable", "accounting invoice is not in a payable state", f"The accounting invoice state is {evidence.invoice_status}.")
        if not context.get("accounting_source_consistent", False):
            blocker("source_mismatch", "invoice does not match the linked accounting source", "The captured invoice and the linked accounting payable disagree on amount, currency, supplier, or lines.")
        if evidence.supplier is None:
            blocker("supplier_missing", "trusted supplier record is missing", "No trusted supplier record was retrieved.")
        if not evidence.purchase_orders:
            blocker("missing_purchase_order", "purchase order evidence is missing", "No purchase order supports this invoice.")
        if not evidence.receipts:
            blocker("missing_receipt", "receipt evidence is missing", "No receipt covers the invoiced lines.")
        if evidence.supplier and not evidence.supplier.approved_wallet:
            blocker("wallet_missing", "trusted supplier wallet is missing", "The supplier record carries no approved wallet address.")
        if evidence.supplier and not evidence.supplier.wallet_verified:
            blocker("wallet_unverified", "supplier wallet is unverified", "A human has not verified the supplier's wallet address.")
        if evidence.supplier and invoice.invoice_payee_address and evidence.supplier.approved_wallet:
            if invoice.invoice_payee_address.lower() != evidence.supplier.approved_wallet.lower():
                blocker("payee_mismatch", "invoice payee differs from supplier's trusted wallet", "The invoice names a different payee; only the supplier record may supply a destination.")
        if context["screening"].status.value != "CLEAR":
            if context.get("screening_handled_by_limit"):
                # Configured as a reduced limit rather than a review: the amount check carries the
                # consequence, so this is context rather than a reason to stop.
                observations.append({
                    "code": "screening_reduced_limit",
                    "severity": "info",
                    "claim": "address screening is unclear",
                    "detail": (
                        f"Address screening returned {context['screening'].status.value}; by configuration this "
                        "reduces the automatic limit instead of requiring a reviewer."
                    ),
                })
            else:
                blocker("screening_not_clear", "address screening is not clear", f"Address screening returned {context['screening'].status.value}.")
        if not context.get("settlement_supported", False):
            blocker("unsupported_currency", "invoice currency has no configured settlement conversion", "The invoice currency has no explicit USDC settlement rate.")

        if context.get("over_limit"):
            judgement("amount_above_automatic_limit", "configured payment limit", "The amount exceeds the automatic limit and needs a human decision.")
        if context.get("reserve_breach"):
            judgement("treasury_reserve_pressure", "treasury balance and reserve", "Paying now would leave less than the configured treasury reserve.")
        if context.get("discount_due"):
            # Supporting information, not a reason to hesitate: an expiring discount argues for
            # paying now, which is what the policy's early_discount check also concludes.
            observations.append({
                "code": "discount_opportunity",
                "severity": "info",
                "claim": "early-payment discount window",
                "detail": "An early-payment discount is expiring inside the payment window.",
            })
        if invoice.due_date > context["today"] + context["due_window"] and not context.get("discount_due"):
            judgement("not_due_yet", "invoice due date", f"The invoice is due {invoice.due_date.isoformat()}, outside the {context['due_window']} day window.")

        if not observations:
            observations.append({
                "code": "evidence_complete",
                "severity": "info",
                "claim": "trusted supplier record",
                "detail": "Supplier, invoice, order, receipt, payee, screening and treasury evidence are all present and consistent.",
            })
        return observations

    @staticmethod
    def amount_usdc(context: dict) -> Decimal:
        return Decimal(context["invoice"].amount_units) / Decimal(10**6)
