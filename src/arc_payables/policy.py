from __future__ import annotations

import hashlib
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .domain import (
    AccountingEvidence,
    ConfidenceAssessment,
    Decision,
    DecisionAction,
    EvidenceRef,
    InvoiceRecord,
    PAYABLE_INVOICE_STATUSES,
    PolicyCheck,
    ScreeningStatus,
    TreasurySnapshot,
    USDC_SCALE,
    is_evm_address,
    units_to_usdc,
)
from .ports import AgentRecommendation, CurrencyConverter
from .risk import effective_limit_units, risk_tier
from .screening import ScreeningResult
from .settings import Settings
from .store import canonical_json

#: Screening status to the policy check that reports it.
SCREENING_CHECK_CODES = {
    ScreeningStatus.FLAGGED: "screening_flagged",
    ScreeningStatus.INCONCLUSIVE: "screening_ambiguous",
    ScreeningStatus.UNAVAILABLE: "screening_unavailable",
}

OVERRIDABLE_CHECKS = {
    "payee_mismatch",
    "wallet_unverified",
    "screening_unavailable",
    "screening_flagged",
    "screening_ambiguous",
    "amount_limit",
    "cash_reserve",
    "missing_purchase_order",
    "missing_receipt",

    "purchase_order_match",
    "receipt_match",
    "invoice_line_total",

}

# ERPNext reports a *submitted* Purchase Order with a business status, never the literal
# "Submitted"; these values were observed on a live v15 sandbox (a fully billed/received order
# reports "Completed", and it reports "To Receive and Bill"/"To Bill" before that). A draft
# order reports "Draft" and a cancelled one "Cancelled", so a submitted state is still
# distinguishable from an unsubmitted one without trusting a docstatus flag alone.
PURCHASE_ORDER_STATUSES_PAYABLE = {
    "SUBMITTED",
    "TO RECEIVE AND BILL",
    "TO BILL",
    "TO RECEIVE",
    "COMPLETED",
    "CLOSED",
}
RECEIPT_STATUSES_PAYABLE = {
    "SUBMITTED",
    "TO BILL",
    "COMPLETED",
    "CLOSED",
}


def accounting_source_consistency(
    invoice: InvoiceRecord,
    accounting: AccountingEvidence,
    converter: CurrencyConverter,
) -> tuple[bool, str]:
    """Single source of truth for "does the accounting payable match this invoice?".

    Used by the deterministic policy (authoritative) and by the advisory agent (informational)
    so the two can never disagree about the conversion, currencies, or line items.
    """
    configured = (getattr(converter, "invoice_currency", "") or "").upper()
    expected_erp_units = converter.invoice_units_from_settlement(invoice.amount_units, configured)
    present = all((
        accounting.source_invoice_amount_units is not None,
        accounting.source_invoice_currency,
        accounting.source_invoice_number,
        accounting.source_supplier_id,
        accounting.source_invoice_id,
        accounting.source_invoice_lines,
    ))
    matches = bool(
        present
        and configured
        and expected_erp_units is not None
        and accounting.source_invoice_amount_units == expected_erp_units
        and str(accounting.source_invoice_currency).upper() == configured
        and accounting.source_invoice_number == invoice.invoice_number
        and accounting.source_supplier_id == invoice.supplier_id
        and accounting.source_invoice_id == (invoice.purchase_invoice_id or invoice.id)
        and _normalized_lines(accounting.source_invoice_lines) == _normalized_lines(invoice.lines)
    )
    if matches:
        detail = (
            "Invoice amount, currency, supplier reference, linked ERP invoice, and line items match "
            f"the accounting source at the configured {configured} settlement rate."
        )
    else:
        detail = "Invoice data does not match a linked, trusted Purchase Invoice in the accounting system."
    return matches, detail


def _normalized_lines(lines) -> list:
    return sorted(
        (line.item_code, Decimal(line.quantity), line.amount_units, line.purchase_order_line_id or "")
        for line in lines
    )


def compute_evidence_hash(invoice: InvoiceRecord, accounting: AccountingEvidence, treasury: TreasurySnapshot, screening: ScreeningResult) -> str:
    supplier = accounting.supplier
    payload = {
        "invoice_id": invoice.id,
        "supplier_id": invoice.supplier_id,
        "invoice_number": invoice.invoice_number,
        "invoice_date": invoice.invoice_date.isoformat(),
        "due_date": invoice.due_date.isoformat(),
        "amount_units": invoice.amount_units,
        "currency": invoice.currency.upper(),
        "candidate_payee": invoice.invoice_payee_address.lower() if invoice.invoice_payee_address else None,
        "source_text_hash": invoice.source_text_hash,
        "source_document_hash": invoice.source_document_hash,
        "lines": [asdict(line) for line in invoice.lines],
        "purchase_order_ids": invoice.purchase_order_ids,
        "receipt_ids": invoice.receipt_ids,
        "discount_percent": invoice.discount_percent,
        "discount_deadline": invoice.discount_deadline.isoformat() if invoice.discount_deadline else None,
        "supplier": asdict(supplier) if supplier else None,
        "purchase_orders": [asdict(po) for po in accounting.purchase_orders],
        "receipts": [asdict(receipt) for receipt in accounting.receipts],
        "duplicate_invoice_id": accounting.duplicate_invoice_id,
        "invoice_status": accounting.invoice_status,
        "source_invoice": {
            "amount_units": accounting.source_invoice_amount_units,
            "currency": accounting.source_invoice_currency,
            "invoice_number": accounting.source_invoice_number,
            "supplier_id": accounting.source_supplier_id,
            "invoice_id": accounting.source_invoice_id,
            "lines": [asdict(line) for line in accounting.source_invoice_lines],
        },
        "screening": {
            "status": screening.status.value,
            "provider": screening.provider,
            "dataset": screening.dataset,
            "wallet": screening.wallet,
            "response_hash": screening.response_hash,
            "matches": [match.to_dict() for match in screening.matches],
        },
        "treasury": {
            "balance_units": treasury.balance_units,
            "evidence_id": treasury.evidence_id,
            "source": treasury.source,
        },
    }
    return "0x" + hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _human_ack(approval: dict | list | None, code: str) -> bool:
    if not approval:
        return False
    approvals = approval if isinstance(approval, list) else [approval]
    if not approvals:
        return False
    latest = approvals[-1]
    return bool(latest.get("approved")) and code in latest.get("acknowledged_checks", [])


class DeterministicPolicy:
    def __init__(self, settings: Settings, converter: CurrencyConverter):
        self.settings = settings
        self.converter = converter

    def evaluate(
        self,
        invoice: InvoiceRecord,
        accounting: AccountingEvidence,
        treasury: TreasurySnapshot,
        screening: ScreeningResult,
        recommendation: AgentRecommendation,
        approval: dict | None = None,
        now: date | None = None,
    ) -> Decision:
        today = now or date.today()
        evidence_hash = compute_evidence_hash(invoice, accounting, treasury, screening)
        approvals = approval if isinstance(approval, list) else ([approval] if approval else [])
        if approvals and approvals[-1].get("evidence_hash") == evidence_hash:
            current_approval = approval
        else:
            current_approval = None
        refs: list[EvidenceRef] = []
        checks: list[PolicyCheck] = []
        missing: list[str] = []
        conflicts: list[str] = []

        def ref(ref_id: str, source: str, field: str, value: str | None = None) -> str:
            refs.append(EvidenceRef(ref_id, source, field, value))
            return ref_id

        inv = ref(f"invoice:{invoice.id}", "invoice_record", "invoice_id", invoice.id)
        amount_ref = ref(f"invoice:{invoice.id}:amount", "invoice_record", "amount", f"{units_to_usdc(invoice.amount_units)} {invoice.currency.upper()}")
        due_ref = ref(f"invoice:{invoice.id}:due", "invoice_record", "due_date", invoice.due_date.isoformat())
        status_ok = accounting.invoice_status.upper() in PAYABLE_INVOICE_STATUSES
        checks.append(PolicyCheck("invoice_payable", status_ok, f"Accounting invoice state is {accounting.invoice_status}.", (inv,)))
        if not status_ok:
            missing.append("A submitted, outstanding Purchase Invoice is required.")

        duplicate_ok = accounting.duplicate_invoice_id is None
        duplicate_refs = (inv,)
        if accounting.duplicate_invoice_id:
            duplicate_refs = (ref(f"erp:purchase-invoice:{accounting.duplicate_invoice_id}", "accounting_connector", "duplicate_invoice", accounting.duplicate_invoice_id),)
            conflicts.append("An existing accounting invoice has the same supplier invoice reference.")
        checks.append(PolicyCheck("duplicate_invoice", duplicate_ok, "No matching duplicate invoice was found." if duplicate_ok else "A duplicate invoice reference exists.", duplicate_refs))

        supplier = accounting.supplier
        if supplier is None:
            checks.append(PolicyCheck("supplier_record", False, "Trusted supplier record is missing.", (), True, True))
            missing.append("Trusted Supplier record.")
            recipient = None
        else:
            supplier_ref = ref(f"supplier:{supplier.id}", "trusted_supplier_record", "supplier_id", supplier.id)
            wallet_ref = ref(f"supplier:{supplier.id}:wallet", "trusted_supplier_record", "approved_wallet", supplier.approved_wallet)
            checks.append(PolicyCheck("supplier_record", True, "Trusted supplier record was retrieved.", (supplier_ref,)))
            if supplier.payment_blocked:
                # Non-overridable on purpose. A supplier the accounting system has blocked is a
                # decision a finance team already took, and this system should not be able to
                # second-guess it, least of all through an approval form.
                checks.append(PolicyCheck(
                    "supplier_blocked",
                    False,
                    f"Payment is blocked by the accounting system: {supplier.blocked_reason or 'the supplier is on hold or disabled'}.",
                    (supplier_ref,),
                    False,
                    False,
                ))
                missing.append("Clear the supplier's hold or disabled state in the accounting system.")
            recipient = supplier.approved_wallet
            wallet_address_valid = is_evm_address(supplier.approved_wallet)
            wallet_ok = wallet_address_valid and supplier.wallet_verified
            wallet_human = wallet_address_valid and not supplier.wallet_verified
            wallet_acknowledged = wallet_human and _human_ack(current_approval, "wallet_unverified")
            wallet_detail = (
                "Approved supplier wallet is verified."
                if wallet_ok
                else (
                    "A reviewer acknowledged that the supplier record's wallet is unverified; the destination"
                    " is still that record's wallet and nothing else."
                    if wallet_acknowledged
                    else "Approved supplier wallet is missing, invalid, or unverified."
                )
            )
            checks.append(PolicyCheck("wallet_unverified", wallet_ok or wallet_acknowledged, wallet_detail, (wallet_ref,), wallet_human or not wallet_address_valid, wallet_human))
            if not supplier.approved_wallet:
                missing.append("Verified approved supplier wallet address.")
            elif not supplier.wallet_verified and not _human_ack(current_approval, "wallet_unverified"):
                missing.append("Human confirmation that the existing supplier-record wallet is the approved payee.")

            candidate_ref: str | None = None
            if invoice.invoice_payee_address:
                candidate_ref = ref(f"invoice:{invoice.id}:payee", "untrusted_invoice_field", "payee_address", invoice.invoice_payee_address)
            if not invoice.invoice_payee_address:
                # No redirect is being attempted, and the destination is taken only from the
                # trusted supplier record, so an absent payee field is not a payment risk.
                checks.append(PolicyCheck(
                    "payee_mismatch",
                    True,
                    "Invoice carries no payee field; the destination is taken only from the trusted supplier record.",
                    (wallet_ref,),
                ))
            else:
                matches = bool(recipient) and invoice.invoice_payee_address.lower() == recipient.lower()
                acknowledged = _human_ack(current_approval, "payee_mismatch")
                checks.append(PolicyCheck("payee_mismatch", matches or acknowledged, "Invoice payee matches the trusted supplier wallet." if matches else ("Reviewer acknowledged the mismatch; destination remains the trusted Supplier record." if acknowledged else "Invoice payee differs from the trusted Supplier wallet; invoice address cannot be a payment destination."), tuple([wallet_ref, candidate_ref] if candidate_ref else [wallet_ref]), not matches, not matches))
                if not matches:
                    conflicts.append("Invoice payee differs from the approved supplier wallet.")
                    if not acknowledged:
                        missing.append("Human review of the invoice/supplier wallet mismatch.")

        invoice_lines_ref = ref(f"invoice:{invoice.id}:lines", "invoice_record", "line_items", str(len(invoice.lines)))
        lines_total = sum(line.amount_units for line in invoice.lines)
        line_total_match = bool(invoice.lines) and lines_total == invoice.amount_units
        line_total_waived = line_total_match or _human_ack(current_approval, "invoice_line_total")
        checks.append(PolicyCheck("invoice_line_total", line_total_waived, "Invoice line amounts equal the invoice total." if line_total_match else ("A reviewer waived the invoice line totals; the billed amount is unchanged." if line_total_waived else "Invoice line amounts are missing or do not equal the invoice total."), (invoice_lines_ref, amount_ref), not line_total_match, not line_total_match))
        if not line_total_match:
            conflicts.append("Invoice line total does not equal the billed amount.")
            missing.append("Reconciled invoice line amounts.")

        source_invoice_ref = ref(
            f"erp:purchase-invoice:{accounting.source_invoice_id or invoice.purchase_invoice_id or invoice.id}",
            "accounting_connector",
            "source_purchase_invoice",
            accounting.source_invoice_id,
        )
        if not accounting.invoice_linked:
            # A captured invoice is not payable until a human matches it to an ERPNext payable.
            # This is a business step, not an exception a reviewer can wave through, so it is
            # reported as its own non-overridable check instead of a data mismatch.
            checks.append(PolicyCheck(
                "erp_link_required",
                False,
                "No ERPNext payable record is linked to this invoice.",
                (inv, source_invoice_ref),
                False,
                False,
            ))
            missing.append(
                "Link this captured invoice to an ERPNext payable record before it can be paid "
                "(POST /invoices/{invoice_id}/link)."
            )
        if accounting.invoice_linked:
            source_invoice_matches, source_invoice_detail = accounting_source_consistency(invoice, accounting, self.converter)
            checks.append(PolicyCheck(
                "accounting_invoice_match",
                source_invoice_matches,
                source_invoice_detail,
                (source_invoice_ref, amount_ref, invoice_lines_ref),
                not source_invoice_matches,
                False,
            ))
            if not source_invoice_matches:
                conflicts.append("Invoice amount, currency, supplier reference, or line items differ from the accounting source.")
                missing.append("Matching linked Purchase Invoice data from the accounting system.")

        po_refs: list[str] = []
        receipt_refs: list[str] = []
        order_items: dict[str, Any] = {}
        for order in accounting.purchase_orders:
            po_ref = ref(f"po:{order.id}", "purchase_order", "order_id", order.id)
            po_refs.append(po_ref)
            for line in order.lines:
                order_items[line.id] = line
        if accounting.purchase_orders:
            supplier_po_match = supplier is not None and all(po.supplier_id == supplier.id and po.status.upper() in PURCHASE_ORDER_STATUSES_PAYABLE for po in accounting.purchase_orders)
            po_lines_match = bool(invoice.lines) and all(
                line.purchase_order_line_id in order_items
                and order_items[line.purchase_order_line_id].item_code == line.item_code
                and Decimal(order_items[line.purchase_order_line_id].quantity) >= Decimal(line.quantity)
                and order_items[line.purchase_order_line_id].amount_units >= line.amount_units
                for line in invoice.lines
            )
            po_ok = supplier_po_match and po_lines_match
            if not po_lines_match:
                conflicts.append("Purchase Order line, item, or amount does not support the invoice.")
            po_waived = po_ok or _human_ack(current_approval, "purchase_order_match")
            checks.append(PolicyCheck("purchase_order_match", po_waived, "Purchase Order supplier and line amounts match." if po_ok else ("A reviewer waived the Purchase Order comparison; the destination and amount are unchanged." if po_waived else "Purchase Order evidence is inconsistent or incomplete."), tuple(po_refs), not po_ok, not po_ok))
        else:
            po_missing_waived = _human_ack(current_approval, "missing_purchase_order")
            checks.append(PolicyCheck("missing_purchase_order", po_missing_waived, "No Purchase Order evidence was found." if not po_missing_waived else "A reviewer waived the missing Purchase Order; the destination and amount are unchanged.", (), True, True))
            missing.append("Purchase Order evidence.")

        for receipt in accounting.receipts:
            receipt_ref = ref(f"receipt:{receipt.id}", "purchase_receipt", "receipt_id", receipt.id)
            receipt_refs.append(receipt_ref)
        if accounting.receipts:
            received: dict[str, Decimal] = {}
            for receipt in accounting.receipts:
                if receipt.status.upper() not in RECEIPT_STATUSES_PAYABLE or (supplier and receipt.supplier_id != supplier.id):
                    conflicts.append(f"Receipt {receipt.id} is not submitted or is for a different supplier.")
                    continue
                for line in receipt.lines:
                    key = line.purchase_order_line_id or f"item:{line.item_code}"
                    received[key] = received.get(key, Decimal(0)) + Decimal(line.quantity)
            receipt_match = bool(invoice.lines) and all(
                received.get(line.purchase_order_line_id or f"item:{line.item_code}", Decimal(0)) >= Decimal(line.quantity)
                for line in invoice.lines
            )
            receipt_waived = receipt_match or _human_ack(current_approval, "receipt_match")
            checks.append(PolicyCheck("receipt_match", receipt_waived, "Receipt quantities cover the invoiced lines." if receipt_match else ("A reviewer waived the receipt comparison; the destination and amount are unchanged." if receipt_waived else "Receipt evidence is missing, incomplete, or conflicts with billed quantities."), tuple(receipt_refs), not receipt_match, not receipt_match))
            if not receipt_match:
                conflicts.append("Receipt quantities do not cover all invoiced quantities.")
        else:
            receipt_missing_waived = _human_ack(current_approval, "missing_receipt")
            checks.append(PolicyCheck("missing_receipt", receipt_missing_waived, "No Purchase Receipt evidence was found." if not receipt_missing_waived else "A reviewer waived the missing Purchase Receipt; the destination and amount are unchanged.", (), True, True))
            missing.append("Purchase Receipt evidence.")

        currency_ok = self.converter.settlement_amount_usdc(invoice.amount_units, invoice.currency) is not None
        checks.append(PolicyCheck("settlement_currency", currency_ok, "Invoice currency is USDC; no implicit FX conversion is applied." if currency_ok else f"{invoice.currency.upper()} has no explicitly configured USDC settlement rate." , (amount_ref,), not currency_ok, False))
        if not currency_ok:
            missing.append("Explicit currency conversion configuration; MVP accepts USDC-denominated invoices only.")

        balance_ref = ref(treasury.evidence_id, treasury.source, "usdc_balance", f"{units_to_usdc(treasury.balance_units)} USDC at {treasury.captured_at.isoformat()}")
        amount_units = self.converter.settlement_amount_usdc(invoice.amount_units, invoice.currency) or 0
        # The automatic limit is scaled by the counterparty's risk tier, so an unclear
        # screening result buys a smaller unattended payment rather than a refusal. Tiering
        # never grants authority: flagged or unavailable screening keeps its own review check.
        tier = risk_tier(screening.status)
        # Risk-tiered limits are opt-in. The default posture requires a human when screening is
        # unclear, and scaling the amount on top would ask for the same risk decision twice.
        # When the operator opts into reduced-limit handling, the screening check below stops
        # requiring a human for the unclear tiers and this check carries the consequence.
        tier_limited = tier == "medium" and self.settings.screening_medium_tier_handling == "limit"
        limit_units = effective_limit_units(self.settings, screening.status) if tier_limited else self.settings.max_invoice_units
        over_limit = amount_units > limit_units
        if limit_units == self.settings.max_invoice_units:
            limit_detail = (
                f"Invoice is within {self.settings.max_invoice_usdc} USDC automatic limit."
                if not over_limit
                else f"Invoice exceeds the configured {self.settings.max_invoice_usdc} USDC automatic limit."
            )
        else:
            limit_detail = (
                f"Invoice is within the reduced {units_to_usdc(limit_units)} USDC automatic limit for risk tier "
                f"{tier} (full limit {self.settings.max_invoice_usdc} USDC)."
                if not over_limit
                else f"Invoice exceeds the reduced {units_to_usdc(limit_units)} USDC automatic limit for risk tier "
                f"{tier} (full limit {self.settings.max_invoice_usdc} USDC)."
            )
        checks.append(PolicyCheck("amount_limit", not over_limit or _human_ack(current_approval, "amount_limit"), limit_detail, (amount_ref,), over_limit, over_limit))
        if over_limit and not _human_ack(current_approval, "amount_limit"):
            missing.append("Human approval for amount above configured automatic limit.")

        reserve_breach = amount_units <= 0 or treasury.balance_units - amount_units < self.settings.min_reserve_units
        checks.append(PolicyCheck("cash_reserve", not reserve_breach or _human_ack(current_approval, "cash_reserve"), f"Post-payment balance preserves the {self.settings.min_reserve_usdc} USDC reserve." if not reserve_breach else f"Payment would leave less than the configured {self.settings.min_reserve_usdc} USDC reserve.", (balance_ref, amount_ref), reserve_breach, reserve_breach))
        if reserve_breach and not _human_ack(current_approval, "cash_reserve"):
            missing.append("Human review of the configured cash-reserve breach.")

        from datetime import datetime, timezone
        age_seconds = (datetime.now(timezone.utc) - treasury.captured_at).total_seconds()
        fresh = 0 <= age_seconds <= self.settings.max_treasury_snapshot_age_seconds
        checks.append(PolicyCheck("treasury_fresh", fresh, "Treasury snapshot is fresh." if fresh else "Treasury snapshot is stale or timestamped in the future." , (balance_ref,), not fresh, False))
        if not fresh:
            missing.append("Current treasury balance snapshot.")

        screen_ref = ref(f"screening:{supplier.id if supplier else invoice.supplier_id}", f"screening_provider:{screening.provider}", "address_screening", screening.status.value)

        code = SCREENING_CHECK_CODES.get(screening.status)
        if code is None:
            checks.append(PolicyCheck(
                "address_screening",
                True,
                screening.reason or "Screening completed with a clear result.",
                (screen_ref,),
            ))
        else:
            # A confirmed risk-topic hit is not something a reviewer clears on the invoice by
            # default: SCREENING_POSITIVE_MATCH_POLICY=review makes it acknowledgeable for
            # operators who verify positives out of band.
            overridable = (
                screening.status != ScreeningStatus.FLAGGED
                or self.settings.screening_positive_match_policy == "review"
            )
            acknowledged = overridable and _human_ack(current_approval, code)
            detail = screening.reason or "Screening requires review."
            if acknowledged:
                detail += (
                    " A reviewer acknowledged this screening exception; the payment destination"
                    " still comes only from the trusted supplier record."
                )
            handles_it_with_a_limit = tier_limited and screening.status in {
                ScreeningStatus.INCONCLUSIVE,
                ScreeningStatus.UNAVAILABLE,
            }
            if handles_it_with_a_limit:
                checks.append(PolicyCheck(
                    code,
                    True,
                    (screening.reason or "Screening is unclear.")
                    + " Handled as a reduced automatic limit by configuration rather than by a reviewer, so this"
                    " counterparty may be paid less without a human in the loop.",
                    (screen_ref,),
                    False,
                    overridable,
                ))
            else:
                checks.append(PolicyCheck(code, acknowledged, detail, (screen_ref,), True, overridable))
                if not acknowledged:
                    missing.append(f"Human review for screening result: {screening.status.value.lower()}.")

        duplicate_ok = duplicate_ok
        if accounting.duplicate_invoice_id:
            missing.append("Resolution of duplicate accounting invoice.")
        if accounting.invoice_status.upper() not in PAYABLE_INVOICE_STATUSES:
            missing.append("A payable, submitted invoice in ERPNext.")

        today_ref = ref(f"treasury:policy:{self.settings.policy_version}", "policy_configuration", "payment_due_window_days", str(self.settings.payment_due_window_days))
        from datetime import timedelta
        discount_due = bool(
            invoice.discount_deadline
            and today <= invoice.discount_deadline <= today + timedelta(days=self.settings.payment_due_window_days)
            and invoice.discount_percent
            and Decimal(invoice.discount_percent) > self.settings.discount_min_percent
        )
        terms_ref = ref(f"invoice:{invoice.id}:terms", "invoice_record", "payment_terms", invoice.payment_terms or "not supplied")
        discount_ref = ref(f"invoice:{invoice.id}:discount", "invoice_record", "discount_deadline", invoice.discount_deadline.isoformat() if invoice.discount_deadline else None)
        if invoice.discount_percent and invoice.discount_deadline and discount_due:
            discount_check = PolicyCheck("early_discount", True, f"A {invoice.discount_percent}% discount is available through {invoice.discount_deadline.isoformat()}.", (terms_ref, discount_ref))
            checks.append(discount_check)
        else:
            checks.append(PolicyCheck("early_discount", True, "No eligible early-payment discount is recorded.", (terms_ref, discount_ref)))
        due_soon = invoice.due_date <= today + timedelta(days=self.settings.payment_due_window_days)
        checks.append(PolicyCheck("payment_timing", due_soon or discount_due, "Invoice is due within the configured payment window or an eligible discount is available." if due_soon or discount_due else "Invoice is not due soon; preserve cash and wait.", (due_ref, terms_ref, discount_ref, today_ref)))

        non_overridable_blockers = []
        for check in checks:
            if check.passed or check.code == "payment_timing":
                continue
            if check.overridable and check.code in OVERRIDABLE_CHECKS:
                continue
            non_overridable_blockers.append(check)
        all_passed = all(check.passed for check in checks)
        manual_checks = [check for check in checks if check.requires_human and not check.passed]
        human_exception_approved = bool(current_approval and approvals and approvals[-1].get("approved") and not manual_checks)

        if not accounting.invoice_linked:
            action = DecisionAction.HOLD
            reason = (
                "Payment is held: this captured invoice is not linked to an ERPNext payable record. "
                "Link it to the accounting payable, then evaluate again."
            )
        elif accounting.duplicate_invoice_id or not status_ok:
            action = DecisionAction.HOLD
            reason = "Payment is held because the accounting record is duplicate or not payable."
        elif non_overridable_blockers and any(check.requires_human for check in non_overridable_blockers):
            action = DecisionAction.ESCALATE
            reason = "Human review or missing configuration is required for a deterministic policy blocker."
        elif non_overridable_blockers:
            action = DecisionAction.HOLD
            reason = "Payment is blocked by a deterministic policy check that human approval cannot override."
        elif manual_checks:
            action = DecisionAction.ESCALATE
            reason = "Human review is required for the listed evidence or policy exceptions."
        elif not due_soon and not discount_due:
            action = DecisionAction.WAIT
            reason = "Invoice is not due within the configured payment window and no eligible early-payment discount is expiring."
        elif all_passed and (recommendation.action == DecisionAction.PAY_NOW.value or human_exception_approved):
            action = DecisionAction.PAY_NOW
            reason = recommendation.reason if recommendation.action == DecisionAction.PAY_NOW.value else "The listed policy exceptions were explicitly approved; payment still uses only the trusted Supplier wallet."
        else:
            action = DecisionAction.ESCALATE
            reason = "The recommendation could not be safely supported by the recorded evidence."
            conflicts.append("The agent recommendation was not supportable from the deterministic policy result.")

        material_check_count = sum(1 for check in checks if check.passed)
        if material_check_count == len(checks):
            level, basis = "HIGH", "All recorded deterministic checks passed; this is a qualitative evidence-coverage label, not a probability."
        elif material_check_count >= len(checks) * 0.7:
            level, basis = "MEDIUM", "Most checks passed, but one or more recorded evidence gaps or human-review checks remain; not a probability."
        else:
            level, basis = "LOW", "Material evidence is missing or conflicting; not a probability."

        unique_refs = {item.id: item for item in refs}
        return Decision(
            action=action,
            reason=reason,
            evidence=tuple(unique_refs.values()),
            missing_evidence=tuple(dict.fromkeys(missing)),
            conflicts=tuple(dict.fromkeys(conflicts)),
            confidence=ConfidenceAssessment(level, basis),
            checks=tuple(checks),
            evidence_hash=evidence_hash,
            policy_version=self.settings.policy_version,
            advisory={
                "decided_by": recommendation.decided_by,
                "action": recommendation.action,
                "fast_path_action": recommendation.fast_path_action,
                "confidence": recommendation.confidence,
                "rationale": recommendation.rationale,
                "material_claims": list(recommendation.material_claims),
                "observation_codes": list(recommendation.observation_codes),
                "evidence_used": list(recommendation.evidence_used),
                "deliberations": [dict(item) for item in recommendation.deliberations],
            },
        )
