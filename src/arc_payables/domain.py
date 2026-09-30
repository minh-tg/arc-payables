from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Any

USDC_DECIMALS = 6
USDC_SCALE = 10**USDC_DECIMALS
ARC_TESTNET_CHAIN_ID = 5_042_002
ARC_TESTNET_USDC = "0x3600000000000000000000000000000000000000"


def is_evm_address(value: str | None) -> bool:
    if not value or len(value) != 42 or not value.startswith("0x"):
        return False
    try:
        return int(value[2:], 16) != 0
    except ValueError:
        return False


def retry_backoff_seconds(attempts: int, *, base: int = 60, cap: int = 3600) -> int:
    """How long to wait before retrying something that has failed `attempts` times.

    Deterministic on purpose: no jitter, so a test can assert the schedule and an operator can read
    it off the record. A misconfigured accounting system should stop filling the log every pass, and
    it should still be retried, because configuration gets fixed.
    """
    if attempts <= 0:
        return 0
    return int(min(base * (2 ** (attempts - 1)), cap))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def usdc_to_units(value: Decimal | str | int) -> int:
    amount = Decimal(str(value))
    scaled = amount * USDC_SCALE
    if scaled != scaled.to_integral_value():
        raise ValueError("USDC amounts support at most 6 decimal places")
    if scaled <= 0:
        raise ValueError("USDC amount must be positive")
    return int(scaled)


def units_to_usdc(value: int) -> str:
    return f"{Decimal(value) / USDC_SCALE:.6f}".rstrip("0").rstrip(".") or "0"


class DecisionAction(StrEnum):
    PAY_NOW = "PAY_NOW"
    WAIT = "WAIT"
    HOLD = "HOLD"
    ESCALATE = "ESCALATE"


class WorkflowState(StrEnum):
    RECEIVED = "RECEIVED"
    EVIDENCE_CHECKING = "EVIDENCE_CHECKING"
    ELIGIBLE = "ELIGIBLE"
    WAITING = "WAITING"
    HELD = "HELD"
    ESCALATED = "ESCALATED"
    AUTHORIZED = "AUTHORIZED"
    SUBMITTED = "SUBMITTED"
    CONFIRMED = "CONFIRMED"
    ERP_PENDING = "ERP_PENDING"
    ERP_RECORDED = "ERP_RECORDED"
    FAILED = "FAILED"
    NEEDS_RECONCILIATION = "NEEDS_RECONCILIATION"


class ScreeningStatus(StrEnum):
    CLEAR = "CLEAR"
    FLAGGED = "FLAGGED"
    UNAVAILABLE = "UNAVAILABLE"
    INCONCLUSIVE = "INCONCLUSIVE"


class PaymentStatus(StrEnum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"
    NOT_FOUND = "NOT_FOUND"


@dataclass(frozen=True)
class InvoiceLine:
    item_code: str
    quantity: str
    amount_units: int
    purchase_order_line_id: str | None = None
    receipt_line_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class InvoiceRecord:
    id: str
    supplier_id: str
    invoice_number: str
    invoice_date: date
    due_date: date
    amount_units: int
    currency: str
    invoice_payee_address: str | None
    lines: tuple[InvoiceLine, ...] = ()
    purchase_invoice_id: str | None = None
    purchase_order_ids: tuple[str, ...] = ()
    receipt_ids: tuple[str, ...] = ()
    discount_percent: str | None = None
    discount_deadline: date | None = None
    payment_terms: str | None = None
    source_text_hash: str | None = None
    source_document_hash: str | None = None
    source_text: str | None = None
    """Raw captured text (for example OCR output). Untrusted: never a destination or an amount,
    and passed to a deliberating layer only as explicitly-labelled data."""
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class SupplierRecord:
    id: str
    name: str
    approved_wallet: str | None
    wallet_verified: bool
    wallet_version: str
    screening: ScreeningStatus = ScreeningStatus.UNAVAILABLE
    erp_supplier_id: str | None = None
    payment_blocked: bool = False
    """True when the accounting system itself blocks this supplier.

    A supplier on hold or disabled in the ERP is a decision a finance team already made, so it is
    read as evidence and never as something this system or a reviewer can override.
    """
    blocked_reason: str | None = None


@dataclass(frozen=True)
class PurchaseOrderLine:
    id: str
    item_code: str
    quantity: str
    amount_units: int


@dataclass(frozen=True)
class PurchaseOrderEvidence:
    id: str
    supplier_id: str
    status: str
    lines: tuple[PurchaseOrderLine, ...]


@dataclass(frozen=True)
class ReceiptLine:
    id: str
    item_code: str
    quantity: str
    purchase_order_line_id: str | None


@dataclass(frozen=True)
class ReceiptEvidence:
    id: str
    supplier_id: str
    status: str
    lines: tuple[ReceiptLine, ...]


@dataclass(frozen=True)
class TreasurySnapshot:
    balance_units: int
    captured_at: datetime
    evidence_id: str
    source: str


@dataclass(frozen=True)
class AccountingEvidence:
    supplier: SupplierRecord | None
    purchase_orders: tuple[PurchaseOrderEvidence, ...]
    receipts: tuple[ReceiptEvidence, ...]
    duplicate_invoice_id: str | None
    invoice_status: str = "SUBMITTED"
    screening: ScreeningStatus = ScreeningStatus.UNAVAILABLE
    invoice_linked: bool = False
    """True only when the connector produced a real accounting payable for this invoice.

    A captured invoice that has not been matched to an ERPNext payable record is never
    payable; the policy reports that as an explicit link step rather than a mismatch.
    """
    source_invoice_amount_units: int | None = None
    source_invoice_currency: str | None = None
    source_invoice_number: str | None = None
    source_supplier_id: str | None = None
    source_invoice_id: str | None = None
    source_invoice_lines: tuple[InvoiceLine, ...] = ()


@dataclass(frozen=True)
class EvidenceRef:
    id: str
    source: str
    field: str
    value: str | None = None


@dataclass(frozen=True)
class PolicyCheck:
    code: str
    passed: bool
    detail: str
    evidence_refs: tuple[str, ...] = ()
    requires_human: bool = False
    overridable: bool = False


@dataclass(frozen=True)
class ConfidenceAssessment:
    level: str
    basis: str


@dataclass(frozen=True)
class Decision:
    action: DecisionAction
    reason: str
    evidence: tuple[EvidenceRef, ...]
    missing_evidence: tuple[str, ...]
    conflicts: tuple[str, ...]
    confidence: ConfidenceAssessment
    checks: tuple[PolicyCheck, ...]
    evidence_hash: str
    policy_version: str
    evaluated_at: datetime = field(default_factory=utcnow)
    advisory: dict[str, Any] = field(default_factory=dict)
    """Which advisory layer produced the recommendation, and on what basis. Recorded for the
    audit; it has no authority over the decision itself."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "reason": self.reason,
            "evidence": [asdict(item) for item in self.evidence],
            "missing_evidence": list(self.missing_evidence),
            "conflicts": list(self.conflicts),
            "confidence": asdict(self.confidence),
            "policy_checks": [asdict(check) for check in self.checks],
            "evidence_hash": self.evidence_hash,
            "policy_version": self.policy_version,
            "evaluated_at": self.evaluated_at.isoformat(),
            "advisory": self.advisory,
        }


@dataclass(frozen=True)
class HumanApproval:
    reviewer: str
    approved: bool
    note: str
    acknowledged_checks: tuple[str, ...]
    created_at: datetime = field(default_factory=utcnow)


@dataclass(frozen=True)
class PaymentPermit:
    payer: str
    token: str
    recipient: str
    amount_units: int
    evidence_hash: str
    payment_id: str
    expiry: int
    chain_id: int
    guard_address: str

    def as_message(self) -> dict[str, Any]:
        return {
            "payer": self.payer,
            "token": self.token,
            "recipient": self.recipient,
            "amount": self.amount_units,
            "evidenceHash": self.evidence_hash,
            "paymentId": self.payment_id,
            "expiry": self.expiry,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "payer": self.payer,
            "token": self.token,
            "recipient": self.recipient,
            "amount_units": self.amount_units,
            "evidence_hash": self.evidence_hash,
            "payment_id": self.payment_id,
            "expiry": self.expiry,
            "chain_id": self.chain_id,
            "guard_address": self.guard_address,
        }


@dataclass(frozen=True)
class PaymentSubmission:
    status: PaymentStatus
    transaction_hash: str | None = None
    provider_transaction_id: str | None = None
    fee_units: int | None = None
    failure_code: str | None = None


@dataclass(frozen=True)
class ERPWriteResult:
    payment_entry_id: str
    docstatus: int
    already_existed: bool = False


def parse_date(value: str) -> date:
    return date.fromisoformat(value)


def serialize_evidence_refs(refs: tuple[EvidenceRef, ...] | list[EvidenceRef]) -> list[dict[str, Any]]:
    return [asdict(ref) for ref in refs]
