from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from dataclasses import asdict, replace
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from .agent import EvidenceDecisionAgent
from .deliberation import build_decision_agent
from .domain import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
    AccountingEvidence,
    Decision,
    DecisionAction,
    InvoiceRecord,
    PaymentPermit,
    PaymentStatus,
    PaymentSubmission,
    ScreeningStatus,
    TreasurySnapshot,
    WorkflowState,
    retry_backoff_seconds,
    units_to_usdc,
    utcnow,
)
from .policy import OVERRIDABLE_CHECKS, DeterministicPolicy, accounting_source_consistency
from .ports import AccountingConnector, AgentRecommendation, PaymentMapping, PaymentProvider, PermitSigner
from .screening import FixtureScreeningProvider, ScreeningResult
from .settings import Settings
from .store import DuplicateInvoiceNumber, ReviewerRevoked, SQLiteEvidenceStore, canonical_json, invoice_to_dict


class WorkflowError(RuntimeError):
    def __init__(self, status_code: int, code: str, message: str):
        self.status_code = status_code
        self.code = code
        super().__init__(message)


def invoice_fingerprint(invoice: InvoiceRecord) -> str:
    raw = invoice_to_dict(invoice)
    raw.pop("created_at", None)
    raw.pop("id", None)
    return hashlib.sha256(canonical_json(raw).encode()).hexdigest()


class APWorkflow:
    def __init__(
        self,
        store: SQLiteEvidenceStore,
        accounting: AccountingConnector,
        payment_provider: PaymentProvider,
        signer: PermitSigner,
        policy: DeterministicPolicy,
        settings: Settings,
        agent: EvidenceDecisionAgent | None = None,
        screener=None,
    ):
        self.store = store
        self.accounting = accounting
        self.payment_provider = payment_provider
        self.signer = signer
        self.policy = policy
        self.settings = settings
        self.agent = agent if agent is not None else build_decision_agent(settings)
        self.screener = screener or FixtureScreeningProvider(store)

    def create_invoice(self, invoice: InvoiceRecord, idempotency_key: str) -> tuple[InvoiceRecord, bool]:
        try:
            return self.store.create_invoice(invoice, idempotency_key)
        except ValueError as exc:
            raise WorkflowError(409, "duplicate_or_idempotency_conflict", str(exc)) from exc

    def import_invoice(self, external_id: str, idempotency_key: str) -> tuple[InvoiceRecord, bool]:
        try:
            invoice, _ = self.accounting.import_invoice(external_id)
        except LookupError as exc:
            raise WorkflowError(404, "accounting_invoice_not_found", "The requested Purchase Invoice was not found.") from exc
        except Exception as exc:
            raise WorkflowError(502, "accounting_read_failed", "ERPNext invoice import failed; no payment was attempted.") from exc
        return self.create_invoice(invoice, idempotency_key)

    def evaluate(self, invoice_id: str) -> dict[str, Any]:
        invoice = self._invoice_or_404(invoice_id)
        self.store.set_state(invoice_id, WorkflowState.EVIDENCE_CHECKING.value, "EVIDENCE_CHECKING_STARTED", {})
        try:
            context = self._load_context(invoice)
            recommendation = self.agent.recommend(self._agent_context(invoice, context))
            approval = self._active_approval(invoice_id)
            decision = self._decide(invoice, context, recommendation, approval)
        except WorkflowError:
            raise
        except Exception as exc:
            self.store.set_state(invoice_id, WorkflowState.NEEDS_RECONCILIATION.value, "EVIDENCE_CHECKING_FAILED", {"error_type": type(exc).__name__})
            raise WorkflowError(503, "evidence_unavailable", "Required accounting or treasury evidence is unavailable; payment remains blocked.") from exc
        state = self._state_for_decision(decision.action)
        snapshot = self._snapshot(invoice, context)
        self.store.save_evaluation(invoice_id, decision.to_dict(), state.value, snapshot)
        return self.get_invoice(invoice_id)

    def _active_approval(self, invoice_id: str, approvals: list | None = None) -> list | None:
        approvals = self.store.get_approval(invoice_id) if approvals is None else approvals
        if not approvals or self.settings.auth_mode != "oidc":
            return approvals
        active = []
        for record in approvals:
            actor = record.get("reviewer_identity") or {}
            if (actor.get("issuer") == self.settings.oidc_issuer
                    and actor.get("mfa_verified") is True
                    and "approver" in self.settings.oidc_subject_roles.get(actor.get("subject"), ())
                    and not self.store.auth_subject_revoked(actor["issuer"], actor["subject"])):
                active.append(record)
        return active

    def approve(self, invoice_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        self._invoice_or_404(invoice_id)
        decision = self.store.get_decision(invoice_id)
        if not decision or decision.get("action") != DecisionAction.ESCALATE.value:
            raise WorkflowError(409, "not_escalated", "Only an escalated invoice can receive human review.")
        required = {
            check["code"]
            for check in decision.get("policy_checks", [])
            if check.get("requires_human") and check.get("overridable") and not check.get("passed")
        }
        supplied = set(approval.get("acknowledged_checks", []))
        if not supplied.issubset(OVERRIDABLE_CHECKS):
            raise WorkflowError(422, "invalid_review_scope", "The approval references an unknown policy check.")
        if not supplied.issubset(required):
            # Acknowledging a check that is not currently reviewable - for example a sanctions
            # hit or a missing ERP link - would record a meaningless approval in the audit
            # trail, so it is rejected outright rather than silently ignored.
            not_reviewable = sorted(supplied - required)
            raise WorkflowError(
                422,
                "invalid_review_scope",
                f"These checks are not human-reviewable for this decision: {', '.join(not_reviewable)}.",
            )
        if approval.get("approved") and not required.issubset(supplied):
            missing = sorted(required - supplied)
            raise WorkflowError(422, "incomplete_review", f"Explicitly acknowledge each required check: {', '.join(missing)}.")
        if not approval.get("approved") and supplied:
            raise WorkflowError(422, "invalid_rejection_scope", "A rejection cannot acknowledge exceptions.")
        record = {
            "reviewer": approval["reviewer"],
            "approved": bool(approval["approved"]),
            "note": approval["note"],
            "acknowledged_checks": sorted(supplied),
            "created_at": utcnow().isoformat(),
            "evidence_hash": decision["evidence_hash"],
        }
        if approval.get("reviewer_identity"):
            record["reviewer_identity"] = approval["reviewer_identity"]
        try:
            self.store.record_approval(invoice_id, record)
        except ReviewerRevoked as exc:
            raise WorkflowError(403, "reviewer_revoked", "Reviewer access changed before approval was recorded.") from exc
        if not record["approved"]:
            self.store.set_state(invoice_id, WorkflowState.HELD.value, "HUMAN_REJECTED", {"reviewer": record["reviewer"]})
            return self.get_invoice(invoice_id)
        return self.evaluate(invoice_id)

    def link_to_purchase_invoice(self, invoice_id: str, purchase_invoice_id: str, reviewer: str) -> dict[str, Any]:
        """Match a captured invoice to an ERPNext payable record.

        The accounting record is the source of truth, so the payable fields (amount, currency,
        lines, order/receipt links, dates, invoice reference) are adopted from it. The captured
        values are preserved in the audit event and any difference is recorded. The captured
        payee field is deliberately kept as untrusted evidence for the payee comparison.

        A reviewer can therefore never make a capture payable by approving it: authorization
        afterwards is bound to the adopted accounting record, the trusted supplier wallet, and
        the deterministic policy result.
        """
        invoice = self._invoice_or_404(invoice_id)
        if self.store.get_payment(invoice_id):
            raise WorkflowError(409, "invoice_already_settled", "This invoice already has an authorization and cannot be relinked.")
        try:
            payable, _evidence = self.accounting.import_invoice(purchase_invoice_id)
        except LookupError as exc:
            raise WorkflowError(404, "accounting_invoice_not_found", "The requested ERPNext Purchase Invoice was not found.") from exc
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(502, "accounting_read_failed", "The ERPNext payable could not be read; nothing was linked.") from exc

        if payable.supplier_id != invoice.supplier_id:
            raise WorkflowError(
                409,
                "supplier_mismatch",
                "The ERPNext payable belongs to a different supplier; a capture cannot be relinked across suppliers.",
            )
        existing = self.store.find_invoice_by_number(payable.supplier_id, payable.invoice_number)
        if existing and existing.id != invoice.id:
            raise WorkflowError(
                409,
                "duplicate_invoice_number",
                "Another captured invoice already uses this supplier invoice number.",
            )

        captured = {
            "invoice_number": invoice.invoice_number,
            "amount": units_to_usdc(invoice.amount_units),
            "currency": invoice.currency,
            "due_date": invoice.due_date.isoformat(),
            "line_count": len(invoice.lines),
            "purchase_order_ids": list(invoice.purchase_order_ids),
            "receipt_ids": list(invoice.receipt_ids),
        }
        differences: list[str] = []
        if invoice.invoice_number != payable.invoice_number:
            differences.append("invoice_number")
        if invoice.amount_units != payable.amount_units:
            differences.append("amount")
        if invoice.currency.upper() != payable.currency.upper():
            differences.append("currency")
        if invoice.due_date != payable.due_date:
            differences.append("due_date")
        if sorted(invoice.purchase_order_ids) != sorted(payable.purchase_order_ids):
            differences.append("purchase_order_ids")
        if sorted(invoice.receipt_ids) != sorted(payable.receipt_ids):
            differences.append("receipt_ids")

        linked = replace(
            invoice,
            invoice_number=payable.invoice_number,
            invoice_date=payable.invoice_date,
            due_date=payable.due_date,
            amount_units=payable.amount_units,
            currency=payable.currency,
            lines=payable.lines,
            purchase_invoice_id=purchase_invoice_id,
            purchase_order_ids=payable.purchase_order_ids,
            receipt_ids=payable.receipt_ids,
            discount_percent=payable.discount_percent,
            discount_deadline=payable.discount_deadline,
            payment_terms=payable.payment_terms,
            # The captured document hashes and the untrusted payee field are retained: the
            # payable is the accounting record, but the capture stays comparable evidence.
            source_text_hash=invoice.source_text_hash,
            source_document_hash=invoice.source_document_hash,
            created_at=invoice.created_at,
        )
        try:
            self.store.update_invoice_record(
                linked,
                "ERP_PAYABLE_LINKED",
                {
                    "reviewer": reviewer,
                    "purchase_invoice_id": purchase_invoice_id,
                    "captured": captured,
                    "captured_amount_authoritative": False,
                    "adopted_fields": [
                        "invoice_number",
                        "invoice_date",
                        "due_date",
                        "amount_units",
                        "currency",
                        "lines",
                        "purchase_order_ids",
                        "receipt_ids",
                    ],
                    "differences": sorted(differences),
                },
            )
        except DuplicateInvoiceNumber as exc:
            raise WorkflowError(
                409,
                "duplicate_invoice_number",
                "Another captured invoice already uses this supplier invoice number.",
            ) from exc
        return self.evaluate(invoice_id)

    def submit_payment(self, invoice_id: str, *, authorization_identity: dict | None = None,
                       authorization_session_hash: str | None = None) -> dict[str, Any]:
        invoice = self._invoice_or_404(invoice_id)
        existing = self.store.get_payment(invoice_id)
        if existing:
            return self._resume_payment(invoice, existing)

        # Read before external evidence. The atomic authorization write checks this revision
        # so concurrent requests cannot spend two obligations against the same old balance.
        authorization_revision = self.store.payment_authorization_revision()
        prior_decision = self.store.get_decision(invoice_id)
        prior_snapshot = self.store.get_evidence_snapshot(invoice_id)
        if not prior_decision or not prior_snapshot:
            raise WorkflowError(409, "evaluation_required", "Evaluate the invoice before authorizing a payment.")
        if prior_decision.get("action") != DecisionAction.PAY_NOW.value:
            raise WorkflowError(409, "payment_not_eligible", "The latest decision does not permit payment.")

        try:
            context = self._load_context(invoice)
            recommendation = self.agent.recommend(self._agent_context(invoice, context))
            approval_snapshot = self.store.get_approval(invoice_id)
            approval = self._active_approval(invoice_id, approval_snapshot)
            fresh_decision = self._decide(invoice, context, recommendation, approval)
        except WorkflowError:
            raise
        except Exception as exc:
            self.store.set_state(invoice_id, WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_PRECHECK_FAILED", {"error_type": type(exc).__name__})
            raise WorkflowError(503, "prepayment_evidence_unavailable", "Fresh evidence could not be confirmed; payment was not submitted.") from exc

        if prior_snapshot.get("source_invoice_hash") != invoice_fingerprint(invoice):
            concurrent = self._raced_payment(invoice)
            if concurrent is not None:
                return concurrent
            state = self._state_for_decision(fresh_decision.action)
            self.store.save_evaluation(invoice_id, fresh_decision.to_dict(), state.value, self._snapshot(invoice, context))
            raise WorkflowError(409, "invoice_changed_after_evaluation", "Invoice data changed after evaluation; review the updated evidence and evaluate again.")
        if prior_decision.get("evidence_hash") != fresh_decision.evidence_hash:
            # Another request may have authorized or settled this invoice, which changes the
            # treasury balance and therefore this decision's evidence. Resuming that payment
            # is correct; a second authorization is not.
            concurrent = self._raced_payment(invoice)
            if concurrent is not None:
                return concurrent
            state = self._state_for_decision(fresh_decision.action)
            self.store.save_evaluation(invoice_id, fresh_decision.to_dict(), state.value, self._snapshot(invoice, context))
            raise WorkflowError(409, "evidence_changed_after_evaluation", "Material evidence changed after evaluation; review the updated decision before payment.")
        if fresh_decision.action != DecisionAction.PAY_NOW:
            self.store.save_evaluation(invoice_id, fresh_decision.to_dict(), self._state_for_decision(fresh_decision.action).value, self._snapshot(invoice, context))
            raise WorkflowError(409, "payment_not_eligible", "Fresh deterministic policy checks do not permit payment.")

        supplier = context["accounting"].supplier
        if not supplier or not supplier.approved_wallet:
            raise WorkflowError(409, "trusted_payee_missing", "A trusted supplier wallet is required; invoice-provided addresses are never used as a destination.")
        amount = self.policy.converter.settlement_amount_usdc(invoice.amount_units, invoice.currency)
        if amount is None:
            raise WorkflowError(409, "unsupported_settlement_currency", "Only USDC-denominated invoices are enabled in this MVP.")
        payer = getattr(self.payment_provider, "wallet_address", None)
        guard = getattr(self.payment_provider, "guard_address", None)
        if not payer or not guard:
            raise WorkflowError(503, "payment_provider_not_configured", "Payment provider is not configured for an authorized payment.")
        # 32 bytes: the permit field and the guard's replay mapping are bytes32. A shorter id
        # would be implicitly padded by the ABI encoder, which hides a layout mistake.
        payment_id = "0x" + secrets.token_hex(32)
        permit = PaymentPermit(
            payer=payer,
            token=ARC_TESTNET_USDC,
            recipient=supplier.approved_wallet,
            amount_units=amount,
            evidence_hash=fresh_decision.evidence_hash,
            payment_id=payment_id,
            expiry=int(time.time()) + self.settings.permit_lifetime_seconds,
            chain_id=ARC_TESTNET_CHAIN_ID,
            guard_address=guard,
        )
        signature = self.signer.sign(permit)
        payment = {
            "payment_id": payment_id,
            "invoice_id": invoice.id,
            "state": WorkflowState.AUTHORIZED.value,
            "idempotency_key": str(uuid.uuid4()),
            "approve_reset_idempotency_key": str(uuid.uuid4()),
            "approve_idempotency_key": str(uuid.uuid4()),
            "payment_idempotency_key": str(uuid.uuid4()),
            "permit": permit.to_dict(),
            "signature": signature,
            "provider_transaction_id": None,
            "provider_stage": None,
            "transaction_hash": None,
            "confirmation_status": "NOT_SUBMITTED",
            "fee_units": None,
            "erp_entry_id": None,
            "erp_fee_status": None,
            "erp_fee_entry_id": None,
            "erp_attempts": 0,
            "erp_next_attempt_at": None,
            "erp_status": "PENDING",
            "decision_evidence_hash": fresh_decision.evidence_hash,
        }
        try:
            payment, created = self.store.create_payment(
                invoice_id, payment, expected_revision=authorization_revision,
                expected_invoice_hash=invoice_fingerprint(invoice), expected_approval=approval_snapshot,
                expected_reviewers=[item["reviewer_identity"] for item in (approval or []) if item.get("reviewer_identity")],
                expected_requester=authorization_identity, expected_session_hash=authorization_session_hash,
            )
        except Exception as exc:
            raise WorkflowError(409, "payment_authorization_conflict", "Treasury or invoice authorization changed, or another settlement is pending; reconcile and evaluate again.") from exc
        if not created:
            return self._resume_payment(invoice, payment)
        self.store.update_payment(invoice_id, {"confirmation_status": "SUBMITTING"}, WorkflowState.SUBMITTED.value, "PAYMENT_SUBMITTED", {"payment_id": payment_id})
        return self._send_existing_permit(invoice, payment)

    def _raced_payment(self, invoice: InvoiceRecord) -> dict[str, Any] | None:
        """Resume a payment another request created while this one was evaluating."""
        raced = self.store.get_payment(invoice.id)
        return self._resume_payment(invoice, raced) if raced else None

    def _resume_payment(self, invoice: InvoiceRecord, payment: dict) -> dict[str, Any]:
        state = payment.get("state")
        if state in {WorkflowState.ERP_RECORDED.value, WorkflowState.CONFIRMED.value, WorkflowState.ERP_PENDING.value}:
            return self.get_invoice(invoice.id)
        if state == WorkflowState.FAILED.value:
            return self.get_invoice(invoice.id)
        try:
            inspected = self.payment_provider.inspect_payment(payment)
        except Exception as exc:
            self.store.update_payment(invoice.id, {"confirmation_status": "UNAVAILABLE"}, WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_RECONCILIATION_UNAVAILABLE", {"error_type": type(exc).__name__})
            return self.get_invoice(invoice.id)
        if inspected.status == PaymentStatus.CONFIRMED:
            if not inspected.transaction_hash:
                self.store.update_payment(invoice.id, {"confirmation_status": "HASH_MISSING"}, WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_CONFIRMATION_HASH_MISSING", {})
                return self.get_invoice(invoice.id)
            return self._confirm_and_writeback(invoice, payment, inspected)
        if inspected.status == PaymentStatus.PENDING:
            self.store.update_payment(invoice.id, self._submission_updates(inspected), WorkflowState.SUBMITTED.value, "PAYMENT_CONFIRMATION_PENDING", {"provider_transaction_id": inspected.provider_transaction_id})
            return self.get_invoice(invoice.id)
        if inspected.status == PaymentStatus.FAILED:
            self.store.update_payment(invoice.id, self._submission_updates(inspected), WorkflowState.FAILED.value, "PAYMENT_FAILED", {"failure_code": inspected.failure_code})
            return self.get_invoice(invoice.id)
        if inspected.status == PaymentStatus.UNCERTAIN:
            self.store.update_payment(invoice.id, self._submission_updates(inspected), WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_RESULT_UNCERTAIN", {"failure_code": inspected.failure_code})
            return self.get_invoice(invoice.id)
        # NOT_FOUND is safe to retry only because inspect_payment checked provider and/or
        # on-chain payment-ID state. The adapter also checks the guard before broadcasting.
        if int(payment["permit"]["expiry"]) <= int(time.time()):
            self.store.update_payment(invoice.id, {"confirmation_status": "EXPIRED_NO_RETRY"}, WorkflowState.NEEDS_RECONCILIATION.value, "EXPIRED_PERMIT_REQUIRES_REVIEW", {})
            return self.get_invoice(invoice.id)
        return self._send_existing_permit(invoice, payment)

    def _send_existing_permit(self, invoice: InvoiceRecord, payment: dict) -> dict[str, Any]:
        def record_transaction(stage: str, provider_tx_id: str) -> None:
            self.store.update_payment(
                invoice.id,
                {"provider_transaction_id": provider_tx_id, "provider_stage": stage, "confirmation_status": f"{stage.upper()}_SUBMITTED"},
                WorkflowState.SUBMITTED.value,
                "PAYMENT_OPERATION_SUBMITTED",
                {"stage": stage, "provider_transaction_id": provider_tx_id},
            )
        try:
            result = self.payment_provider.submit_authorized(payment, on_transaction=record_transaction)
        except Exception as exc:
            state = WorkflowState.NEEDS_RECONCILIATION if getattr(exc, "uncertain", True) else WorkflowState.FAILED
            self.store.update_payment(invoice.id, {"confirmation_status": "UNCERTAIN" if state == WorkflowState.NEEDS_RECONCILIATION else "FAILED", "failure_code": getattr(exc, "code", type(exc).__name__)}, state.value, "PAYMENT_SUBMISSION_ERROR", {"error_type": type(exc).__name__, "uncertain": state == WorkflowState.NEEDS_RECONCILIATION})
            return self.get_invoice(invoice.id)
        if result.status == PaymentStatus.CONFIRMED:
            if not result.transaction_hash:
                self.store.update_payment(invoice.id, self._submission_updates(result) | {"confirmation_status": "HASH_MISSING"}, WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_CONFIRMATION_HASH_MISSING", {})
                return self.get_invoice(invoice.id)
            return self._confirm_and_writeback(invoice, payment, result)
        if result.status == PaymentStatus.PENDING:
            self.store.update_payment(invoice.id, self._submission_updates(result) | {"confirmation_status": "PENDING"}, WorkflowState.SUBMITTED.value, "PAYMENT_CONFIRMATION_PENDING", {"provider_transaction_id": result.provider_transaction_id})
        elif result.status == PaymentStatus.FAILED:
            self.store.update_payment(invoice.id, self._submission_updates(result) | {"confirmation_status": "FAILED"}, WorkflowState.FAILED.value, "PAYMENT_FAILED", {"failure_code": result.failure_code})
        else:
            self.store.update_payment(invoice.id, self._submission_updates(result) | {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value, "PAYMENT_RESULT_UNCERTAIN", {"failure_code": result.failure_code})
        return self.get_invoice(invoice.id)

    def _confirm_and_writeback(self, invoice: InvoiceRecord, payment: dict, result: PaymentSubmission) -> dict[str, Any]:
        self.store.update_payment(
            invoice.id,
            self._submission_updates(result) | {"confirmation_status": "CONFIRMED", "settled_at": utcnow().isoformat(), "erp_status": "PENDING"},
            WorkflowState.CONFIRMED.value,
            "PAYMENT_CONFIRMED",
            {"transaction_hash": result.transaction_hash, "payment_id": payment["payment_id"]},
        )
        confirmed_payment = self.store.get_payment(invoice.id) or payment
        self._record_erp(invoice, confirmed_payment)
        return self.get_invoice(invoice.id)

    def retry_erp_writeback(self, invoice_id: str) -> dict[str, Any]:
        invoice = self._invoice_or_404(invoice_id)
        payment = self.store.get_payment(invoice_id)
        if not payment or payment.get("confirmation_status") != "CONFIRMED" or not payment.get("transaction_hash"):
            raise WorkflowError(409, "settlement_not_confirmed", "ERPNext writeback is allowed only after Arc settlement is confirmed.")
        if payment.get("state") == WorkflowState.ERP_RECORDED.value:
            return self.get_invoice(invoice_id)
        outcome = self._record_erp(invoice, payment)
        if outcome == "IN_PROGRESS":
            raise WorkflowError(409, "erp_writeback_in_progress", "An ERPNext writeback is already in flight for this payment; retry after the lease expires.")
        return self.get_invoice(invoice_id)

    def _reread_settlement_fee(self, invoice: InvoiceRecord, payment: dict) -> dict[str, Any] | None:
        """Ask the provider for a network fee it could not name when the payment settled.

        A provider often cannot report the fee while the transfer is still being indexed. The record
        is written with no fee, the writeback has nothing to book, and the payment would sit
        unrecorded for good, because retrying asks the same unanswerable question. Asking the
        provider again is all this does.

        It changes no amount, destination or confirmation, and a provider that still has no answer
        leaves the record exactly as it was, so the caller can go on deciding what that means.
        """
        if not payment.get("transaction_hash"):
            return None
        try:
            submission = self.payment_provider.inspect_payment(payment)
        except Exception:
            # The provider is the thing that failed. That is not a reason to change the record.
            return None
        if submission.fee_units is None:
            return None
        # Merge rather than replace: the re-read answers only for the settlement operation, and the
        # allowance operations that preceded it were already measured. Overwriting would drop them
        # and understate the cost this deployment absorbed.
        breakdown = dict(payment.get("fee_breakdown") or {})
        breakdown["guard"] = int(submission.fee_units)
        total = sum(breakdown.values())
        self.store.update_payment(
            invoice.id,
            {"fee_units": total, "fee_breakdown": breakdown},
            payment.get("state") or WorkflowState.ERP_PENDING.value,
            "SETTLEMENT_FEE_REREAD",
            {"transaction_hash": payment.get("transaction_hash"), "fee_units": total, "fee_breakdown": breakdown},
        )
        return self.store.get_payment(invoice.id)

    def _record_erp(self, invoice: InvoiceRecord, payment: dict) -> str:
        """Write the settlement into the accounting system: the payment, then the fee we absorbed.

        Two documents, one writeback. The Payment Entry carries exactly the supplier's amount, and the
        network fee is a separate expense entry keyed to the same transaction. If the second write
        fails, the first is remembered, so a retry books only what is missing rather than resubmitting
        the payment.
        """
        is_frappe = self.settings.accounting_provider == "frappe"
        if is_frappe and payment.get("fee_units") is None:
            # Before concluding the fee is unknowable, ask once more. This is the only thing that can
            # move a payment disabled for a missing fee, and without it no retry ever can.
            payment = self._reread_settlement_fee(invoice, payment) or payment
        fee_units = int(payment.get("fee_units") or 0)
        fee_outstanding = fee_units > 0 and payment.get("erp_fee_status") != "RECORDED"
        if payment.get("erp_entry_id") and not fee_outstanding:
            self.store.update_payment(invoice.id, {"erp_status": "RECORDED", "erp_claimed_at": None}, WorkflowState.ERP_RECORDED.value, "ERP_WRITEBACK_ALREADY_RECORDED", {"payment_entry_id": payment["erp_entry_id"]})
            return "ALREADY_RECORDED"
        if is_frappe and not self.settings.frappe_accounting_ready:
            self.store.update_payment(invoice.id, {"erp_status": "DISABLED", "erp_error_code": "ACCOUNTING_MAPPING_INCOMPLETE"}, WorkflowState.ERP_PENDING.value, "ERP_WRITEBACK_DISABLED", {"reason": "accounting mapping not configured"})
            return "DISABLED"
        if is_frappe and payment.get("fee_units") is None:
            # A fee the provider cannot name is a deferral, not a failure. Back off like a failed
            # writeback rather than re-reading the provider on every pass and writing an event each
            # time, and leave it retryable so the payment is not stranded in the ledger's debt.
            attempts = int(payment.get("erp_attempts") or 0)
            self.store.update_payment(
                invoice.id,
                {
                    "erp_status": "DISABLED",
                    "erp_error_code": "NETWORK_FEE_UNAVAILABLE",
                    "erp_attempts": attempts + 1,
                    "erp_next_attempt_at": self._next_writeback_attempt(attempts + 1),
                    "erp_claimed_at": None,
                },
                WorkflowState.ERP_PENDING.value,
                "ERP_WRITEBACK_DISABLED",
                {"reason": "confirmed network fee was not returned by provider", "attempts": attempts + 1},
            )
            return "DISABLED"
        if not self.store.claim_erp_writeback(invoice.id, lease_seconds=self.settings.writeback_lease_seconds):
            return "IN_PROGRESS"
        mapping = self._payment_mapping()

        payment_entry_id = payment.get("erp_entry_id")
        already_existed = False
        if not payment_entry_id:
            try:
                result = self.accounting.create_payment_entry(invoice, payment["transaction_hash"], mapping)
            except Exception as exc:
                return self._record_erp_failure(invoice, payment, exc, "erp_status")
            payment_entry_id = result.payment_entry_id
            already_existed = result.already_existed

        fee_entry_id = payment.get("erp_fee_entry_id")
        if fee_outstanding:
            try:
                fee_result = self.accounting.create_fee_expense(invoice, payment["transaction_hash"], fee_units, mapping)
            except Exception as exc:
                # The payment is in the ledger; only the fee expense is missing. Record the payment
                # entry so the retry cannot book it twice, and leave the invoice short of ERP_RECORDED.
                uncertain = bool(getattr(exc, "uncertain", False))
                attempts = int(payment.get("erp_attempts") or 0)
                updates = {
                    "erp_status": "RECORDED",
                    "erp_entry_id": payment_entry_id,
                    "erp_claimed_at": None,
                    "erp_fee_status": "UNKNOWN" if uncertain else "FAILED",
                    "erp_fee_error_code": str(getattr(exc, "status_code", None) or getattr(exc, "code", None) or type(exc).__name__),
                }
                if uncertain:
                    updates["erp_next_attempt_at"] = (
                        utcnow() + timedelta(seconds=self.settings.writeback_lease_seconds)
                    ).isoformat()
                else:
                    updates["erp_attempts"] = attempts + 1
                    updates["erp_next_attempt_at"] = self._next_writeback_attempt(attempts + 1)
                self.store.update_payment(
                    invoice.id,
                    updates,
                    WorkflowState.ERP_PENDING.value,
                    "ERP_FEE_EXPENSE_FAILED",
                    {
                        "payment_entry_id": payment_entry_id,
                        "error_type": type(exc).__name__,
                        "attempts": updates.get("erp_attempts", attempts),
                        "next_attempt_at": updates.get("erp_next_attempt_at"),
                    },
                )
                return "FEE_FAILED"
            fee_entry_id = fee_result.payment_entry_id if fee_result else None

        updates = {
            "erp_status": "RECORDED",
            "erp_entry_id": payment_entry_id,
            "erp_claimed_at": None,
            "erp_fee_status": "RECORDED" if fee_units > 0 else "NOT_APPLICABLE",
            "erp_fee_entry_id": fee_entry_id,
            "erp_attempts": 0,
            "erp_next_attempt_at": None,
        }
        self.store.update_payment(
            invoice.id,
            updates,
            WorkflowState.ERP_RECORDED.value,
            "ERP_WRITEBACK_RECORDED",
            {
                "payment_entry_id": payment_entry_id,
                "already_existed": already_existed,
                "fee_entry_id": fee_entry_id,
                "fee_units": fee_units,
            },
        )
        return "RECORDED"

    def _record_erp_failure(self, invoice: InvoiceRecord, payment: dict, exc: Exception, status_field: str) -> str:
        """Record a failed writeback, and decide when it is worth trying again.

        An uncertain failure is not the same as a refusal. The write may have committed, so the retry
        must be soon and must reconcile rather than assume; that is why only a definite failure backs
        off, and why the lease is kept for an uncertain one.
        """
        error_code = getattr(exc, "status_code", None) or getattr(exc, "code", None) or type(exc).__name__
        uncertain = bool(getattr(exc, "uncertain", False))
        attempts = int(payment.get("erp_attempts") or 0)
        updates = {status_field: "UNKNOWN" if uncertain else "PENDING", "erp_error_code": str(error_code)}
        if uncertain:
            # The write may have committed and the remote may still be working. Keep the claim, and
            # come back when it expires rather than racing the call whose outcome is unknown.
            updates["erp_attempts"] = attempts
            updates["erp_next_attempt_at"] = (utcnow() + timedelta(seconds=self.settings.writeback_lease_seconds)).isoformat()
        else:
            updates["erp_attempts"] = attempts + 1
            updates["erp_next_attempt_at"] = self._next_writeback_attempt(attempts + 1)
            updates["erp_claimed_at"] = None
        self.store.update_payment(
            invoice.id,
            updates,
            WorkflowState.ERP_PENDING.value,
            "ERP_WRITEBACK_UNCERTAIN" if uncertain else "ERP_WRITEBACK_FAILED",
            {
                "error_type": type(exc).__name__,
                "error_code": str(error_code),
                "uncertain": uncertain,
                "attempts": updates["erp_attempts"],
                "next_attempt_at": updates.get("erp_next_attempt_at"),
            },
        )
        return "UNCERTAIN" if uncertain else "FAILED"

    def _next_writeback_attempt(self, attempts: int) -> str:
        delay = retry_backoff_seconds(
            attempts,
            base=self.settings.writeback_backoff_seconds,
            cap=self.settings.writeback_backoff_max_seconds,
        )
        return (utcnow() + timedelta(seconds=delay)).isoformat()

    def _payment_mapping(self) -> PaymentMapping:
        if self.settings.accounting_provider == "frappe":
            return PaymentMapping(
                company=self.settings.frappe_company or "",
                paid_from=self.settings.frappe_paid_from_account or "",
                paid_to=self.settings.frappe_paid_to_account or "",
                mode_of_payment=self.settings.frappe_mode_of_payment or "",
                source_currency=self.settings.frappe_settlement_currency or "",
                target_currency=self.settings.frappe_target_currency or "",
                company_currency=self.settings.frappe_company_currency or "",
                invoice_currency=self.settings.frappe_invoice_currency or "",
                source_exchange_rate=str(self.settings.frappe_source_exchange_rate),
                target_exchange_rate=str(self.settings.frappe_target_exchange_rate),
                fee_account=self.settings.frappe_fee_account or "",
                fee_currency=self.settings.frappe_fee_currency or "",
                cost_center=self.settings.frappe_cost_center or "",
            )
        # Mock accounting mirrors the same shape so the fee logic is exercised identically.
        return PaymentMapping(
            company="Demo Company",
            paid_from="USDC Wallet - Demo",
            paid_to="Accounts Payable - Demo",
            mode_of_payment="Arc Testnet",
            source_currency="USDC",
            target_currency="USD",
            company_currency="USD",
            invoice_currency="USD",
            source_exchange_rate="1",
            target_exchange_rate="1",
            fee_account="Network Fees - Demo",
            fee_currency="USD",
            cost_center="Main - Demo",
        )

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        invoice = self._invoice_or_404(invoice_id)
        state = self.store.get_state(invoice_id) or WorkflowState.RECEIVED.value
        return {
            "invoice": {
                "id": invoice.id,
                "supplier_id": invoice.supplier_id,
                "invoice_number": invoice.invoice_number,
                "invoice_date": invoice.invoice_date.isoformat(),
                "due_date": invoice.due_date.isoformat(),
                "amount": units_to_usdc(invoice.amount_units),
                "currency": invoice.currency,
                "invoice_payee_address": invoice.invoice_payee_address,
                "purchase_invoice_id": invoice.purchase_invoice_id,
                "purchase_order_ids": list(invoice.purchase_order_ids),
                "receipt_ids": list(invoice.receipt_ids),
                "source_document_hash": invoice.source_document_hash,
                "source_text_hash": invoice.source_text_hash,
            },
            "state": state,
            "decision": self.store.get_decision(invoice_id),
            "evidence": self.store.get_evidence_snapshot(invoice_id),
            "approvals": self.store.get_approval(invoice_id) or [],
            "payment": self._public_payment(self.store.get_payment(invoice_id)),
            "next_actions": self._next_actions(invoice_id, state),
        }

    def _next_actions(self, invoice_id: str, state: str) -> list[dict[str, Any]]:
        """Machine-readable guidance for the operator or UI. Advisory only."""
        decision = self.store.get_decision(invoice_id)
        payment = self.store.get_payment(invoice_id)
        checks = {check["code"]: check for check in (decision or {}).get("policy_checks", [])}
        base = f"/invoices/{invoice_id}"

        if not decision:
            return [{"action": "evaluate", "method": "POST", "path": f"{base}/evaluate", "reason": "No decision has been recorded yet."}]
        if state in {WorkflowState.ERP_RECORDED.value}:
            return []
        if payment and payment.get("confirmation_status") == "CONFIRMED" and payment.get("erp_status") != "RECORDED":
            return [{
                "action": "retry_erp_writeback",
                "method": "POST",
                "path": f"{base}/payment/erp-writeback",
                "reason": "Settlement is confirmed but the ERPNext writeback has not completed. This cannot resubmit funds.",
            }]
        if payment and state == WorkflowState.NEEDS_RECONCILIATION.value:
            return [{
                "action": "reconcile_payment",
                "method": "POST",
                "path": f"{base}/payment/reconcile",
                "reason": "Payment outcome is uncertain; reconcile against the provider and on-chain state.",
            }]
        if payment:
            return [{
                "action": "inspect_payment",
                "method": "GET",
                "path": base,
                "reason": f"A payment is already {payment.get('state')}.",
            }]

        failed = [code for code, check in checks.items() if not check.get("passed")]
        if "erp_link_required" in failed:
            return [{
                "action": "link_erp_invoice",
                "method": "POST",
                "path": f"{base}/link",
                "reason": "Link this captured invoice to an ERPNext payable before it can be paid.",
                "requires_human_token": True,
                "body": {"purchase_invoice_id": "<ERPNext Purchase Invoice name>", "reviewer": "<name>"},
            }]
        if decision["action"] == DecisionAction.PAY_NOW.value:
            return [{
                "action": "authorize_payment",
                "method": "POST",
                "path": f"{base}/payment",
                "reason": "Deterministic checks passed; a one-time exact authorization can be issued.",
            }]
        if decision["action"] == DecisionAction.ESCALATE.value:
            reviewable = sorted(
                check["code"] for check in decision["policy_checks"]
                if check.get("requires_human") and check.get("overridable") and not check.get("passed")
            )
            if reviewable:
                return [{
                    "action": "review",
                    "method": "POST",
                    "path": f"{base}/approval",
                    "reason": "A human must acknowledge each listed exception.",
                    "requires_human_token": True,
                    "acknowledged_checks": reviewable,
                    "note": "A reviewer cannot change the payment destination.",
                }]
            return [{
                "action": "resolve_blocking_evidence",
                "method": None,
                "path": None,
                "reason": "Blocked by evidence that human approval cannot override: " + ", ".join(sorted(failed)),
            }]
        if decision["action"] == DecisionAction.WAIT.value:
            return [{
                "action": "wait",
                "method": None,
                "path": None,
                "reason": decision.get("reason", "Invoice is not due yet."),
            }]
        return [{
            "action": "resolve_blocking_evidence",
            "method": None,
            "path": None,
            "reason": decision.get("reason", "Payment is held."),
        }]

    def list_invoices(self) -> list[dict[str, Any]]:
        return [self.get_invoice(invoice.id) for invoice, _ in self.store.list_invoices()]

    def events(self, invoice_id: str) -> list[dict[str, Any]]:
        self._invoice_or_404(invoice_id)
        return self.store.events(invoice_id)

    def _load_context(self, invoice: InvoiceRecord) -> dict[str, Any]:
        accounting = self.accounting.get_invoice_evidence(invoice)
        recipient = accounting.supplier.approved_wallet if accounting.supplier else None
        try:
            screening = self.screener.screen(accounting.supplier, recipient)
        except Exception:
            screening = ScreeningResult(
                status=ScreeningStatus.UNAVAILABLE,
                provider=getattr(self.screener, "name", "unknown"),
                subject=accounting.supplier.name if accounting.supplier else "unknown",
                wallet=recipient,
                reason="The screening provider raised an error; payment remains blocked.",
            )
        treasury = self.payment_provider.get_balance()
        amount = self.policy.converter.settlement_amount_usdc(invoice.amount_units, invoice.currency) or 0
        today = date.today()
        discount_due = bool(
            invoice.discount_deadline
            and today <= invoice.discount_deadline <= today + timedelta(days=self.settings.payment_due_window_days)
            and invoice.discount_percent
            and Decimal(invoice.discount_percent) > self.settings.discount_min_percent
        )
        return {
            "accounting": accounting,
            "screening": screening,
            "treasury": treasury,
            # Derived once, from the same tier-aware limit the policy applies, so the advisory
            # layer and the authoritative gate can never disagree about the amount.
            "over_limit": amount > self._automatic_limit_units(screening),
            "screening_handled_by_limit": self._screening_handled_by_limit(screening),
            "reserve_breach": amount <= 0 or treasury.balance_units - amount < self.settings.min_reserve_units,
            "discount_due": discount_due,
            "settlement_supported": self.policy.converter.settlement_amount_usdc(invoice.amount_units, invoice.currency) is not None,
            "accounting_source_consistent": accounting_source_consistency(invoice, accounting, self.policy.converter)[0],
        }

    def _automatic_limit_units(self, screening) -> int:
        """The automatic limit for this counterparty's risk tier, shared with the policy."""
        from .risk import effective_limit_units, risk_tier

        tier_limited = (
            risk_tier(screening.status) == "medium" and self.settings.screening_medium_tier_handling == "limit"
        )
        return effective_limit_units(self.settings, screening.status) if tier_limited else self.settings.max_invoice_units

    def _screening_handled_by_limit(self, screening) -> bool:
        """True when an unclear screening result is handled by a reduced limit, not a reviewer."""
        from .domain import ScreeningStatus
        from .risk import risk_tier

        return bool(
            self.settings.screening_medium_tier_handling == "limit"
            and risk_tier(screening.status) == "medium"
            and screening.status in {ScreeningStatus.INCONCLUSIVE, ScreeningStatus.UNAVAILABLE}
        )

    def _agent_context(self, invoice: InvoiceRecord, context: dict[str, Any]) -> dict[str, Any]:
        return {
            "invoice": invoice,
            "accounting": context["accounting"],
            "screening": context["screening"],
            "treasury": context["treasury"],
            "over_limit": context["over_limit"],
            "reserve_breach": context["reserve_breach"],
            "discount_due": context["discount_due"],
            "settlement_supported": context["settlement_supported"],
            "accounting_source_consistent": context["accounting_source_consistent"],
            "screening_handled_by_limit": context.get("screening_handled_by_limit", False),
            "today": date.today(),
            "due_window": timedelta(days=self.settings.payment_due_window_days),
            "automatic_limit_usdc": self.settings.max_invoice_usdc,
            "reserve_floor_usdc": self.settings.min_reserve_usdc,
        }

    def _decide(self, invoice: InvoiceRecord, context: dict[str, Any], recommendation: AgentRecommendation, approval: dict | None) -> Decision:
        return self.policy.evaluate(
            invoice,
            context["accounting"],
            context["treasury"],
            context["screening"],
            recommendation,
            approval,
        )

    @staticmethod
    def _snapshot(invoice: InvoiceRecord, context: dict[str, Any]) -> dict[str, Any]:
        evidence: AccountingEvidence = context["accounting"]
        supplier = evidence.supplier
        treasury: TreasurySnapshot = context["treasury"]
        return {
            "source_invoice_hash": invoice_fingerprint(invoice),
            "supplier": {
                "id": supplier.id,
                "name": supplier.name,
                "approved_wallet": supplier.approved_wallet,
                "wallet_verified": supplier.wallet_verified,
                "wallet_version": supplier.wallet_version,
            } if supplier else None,
            "purchase_orders": [
                {"id": po.id, "supplier_id": po.supplier_id, "status": po.status, "lines": [line.__dict__ for line in po.lines]}
                for po in evidence.purchase_orders
            ],
            "receipts": [
                {"id": receipt.id, "supplier_id": receipt.supplier_id, "status": receipt.status, "lines": [line.__dict__ for line in receipt.lines]}
                for receipt in evidence.receipts
            ],
            "duplicate_invoice_id": evidence.duplicate_invoice_id,
            "invoice_status": evidence.invoice_status,
            "accounting_source_invoice": {
                "id": evidence.source_invoice_id,
                "number": evidence.source_invoice_number,
                "supplier_id": evidence.source_supplier_id,
                "amount_units": evidence.source_invoice_amount_units,
                "currency": evidence.source_invoice_currency,
                "lines": [asdict(line) for line in evidence.source_invoice_lines],
            },
            "screening": context["screening"].to_dict(),
            "treasury": {
                "balance_units": treasury.balance_units,
                "balance_usdc": units_to_usdc(treasury.balance_units),
                "captured_at": treasury.captured_at.isoformat(),
                "evidence_id": treasury.evidence_id,
                "source": treasury.source,
            },
        }

    def _invoice_or_404(self, invoice_id: str) -> InvoiceRecord:
        invoice = self.store.get_invoice(invoice_id)
        if not invoice:
            raise WorkflowError(404, "invoice_not_found", "Invoice was not found.")
        return invoice

    @staticmethod
    def _state_for_decision(action: DecisionAction) -> WorkflowState:
        return {
            DecisionAction.PAY_NOW: WorkflowState.ELIGIBLE,
            DecisionAction.WAIT: WorkflowState.WAITING,
            DecisionAction.HOLD: WorkflowState.HELD,
            DecisionAction.ESCALATE: WorkflowState.ESCALATED,
        }[action]

    @staticmethod
    def _submission_updates(result: PaymentSubmission) -> dict[str, Any]:
        return {
            "provider_transaction_id": result.provider_transaction_id,
            "transaction_hash": result.transaction_hash,
            "fee_units": result.fee_units,
            "fee_breakdown": result.fee_breakdown,
            "failure_code": result.failure_code,
        }

    @staticmethod
    def _public_payment(payment: dict | None) -> dict | None:
        if not payment:
            return None
        # Signatures are not secrets, but they are not needed by invoice readers. Do not
        # return reusable authorization material from the public status endpoint.
        public = {
            key: payment.get(key)
            for key in (
                "payment_id",
                "state",
                "transaction_hash",
                "provider_transaction_id",
                "provider_stage",
                "confirmation_status",
                "fee_units",
                "fee_breakdown",
                "erp_status",
                "erp_entry_id",
                "erp_fee_status",
                "erp_fee_entry_id",
                "failure_code",
            )
            if key in payment
        }
        permit = payment.get("permit") or {}
        if permit:
            public["permit_id"] = permit.get("payment_id")
            public["permit_expires_at"] = permit.get("expiry")
            public["permit_evidence_hash"] = permit.get("evidence_hash")
        return public
