from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Callable, Protocol

from .domain import (
    AccountingEvidence,
    ERPWriteResult,
    InvoiceRecord,
    PaymentPermit,
    PaymentSubmission,
    ScreeningStatus,
    TreasurySnapshot,
)


@dataclass(frozen=True)
class PaymentMapping:
    """ERPNext field mapping. Names follow the Payment Entry fields they populate.

    Only the settlement account carries a non-company currency; the payable account is kept
    in the company currency so a payment has exactly one FX leg and can be verified exactly.
    """

    company: str
    paid_from: str
    paid_to: str
    mode_of_payment: str
    source_currency: str
    """Currency of the paid-from (settlement) account."""

    target_currency: str
    """Currency of the party payable account. Must equal ``company_currency``."""

    company_currency: str
    """Company base currency, which is also the invoice currency in this mapping."""

    invoice_currency: str
    """Currency of the Purchase Invoice and its allocated amount."""

    source_exchange_rate: str
    """Company-currency units per one settlement unit (USD per USDC in the demo)."""

    target_exchange_rate: str
    """Company-currency units per one party-currency unit."""

    fee_account: str
    """Network-fee expense account. Must be in the company currency."""

    cost_center: str
    """Cost centre the network fee is booked against.

    ERPNext requires one on a deduction row, and it refuses to guess, so it is configured and
    verified like every other accounting value rather than left to a default."""

    fee_currency: str
    """Expected currency of the network-fee account. Must equal ``company_currency``."""


@dataclass(frozen=True)
class AgentRecommendation:
    """An advisory opinion. It is never sufficient to authorize a payment.

    ``decided_by`` and ``deliberations`` exist so the audit record can answer which layer
    reached the conclusion, on what basis, and - for a deliberating layer - with which model
    and prompt. The deterministic policy remains authoritative regardless of what is here.
    """

    action: str
    reason: str
    material_claims: tuple[str, ...]
    decided_by: str = "unconfigured"
    rationale: str = ""
    confidence: str | None = None
    evidence_used: tuple[str, ...] = ()
    deliberations: tuple[dict, ...] = ()
    fast_path_action: str | None = None
    observation_codes: tuple[str, ...] = ()
    """Machine-readable codes for the observations behind this recommendation, so a later layer
    can tell a judgement call apart from a missing fact without parsing prose."""


@dataclass(frozen=True)
class Receivable:
    """Money expected in: an accounting-system sales invoice, not a payment the agent made.

    Inflows never authorize anything. The forecast adds them to the running balance on
    their expected date so coverage answers what will be there, not just what will leave.
    """

    external_id: str
    customer: str
    reference: str
    amount_units: int
    currency: str
    expected_date: date
    source: str = "erp"
    collected_at: str | None = None


class ReceivablesProvider(Protocol):
    def list_receivables(self) -> list[Receivable]: ...


class EvidenceStore(Protocol):
    def initialize(self) -> None: ...
    def create_invoice(self, invoice: InvoiceRecord, idempotency_key: str) -> tuple[InvoiceRecord, bool]: ...
    def get_invoice(self, invoice_id: str) -> InvoiceRecord | None: ...
    def list_invoices(self) -> list[tuple[InvoiceRecord, str]]: ...
    def save_evaluation(self, invoice_id: str, decision: dict, state: str, evidence_snapshot: dict) -> None: ...
    def get_decision(self, invoice_id: str) -> dict | None: ...
    def get_evidence_snapshot(self, invoice_id: str) -> dict | None: ...
    def get_state(self, invoice_id: str) -> str | None: ...
    def set_state(self, invoice_id: str, state: str, event_type: str, payload: dict | None = None) -> None: ...
    def record_approval(self, invoice_id: str, approval: dict) -> None: ...
    def get_approval(self, invoice_id: str) -> dict | None: ...
    def create_payment(self, invoice_id: str, payment: dict) -> tuple[dict, bool]: ...
    def get_payment(self, invoice_id: str) -> dict | None: ...
    def update_payment(self, invoice_id: str, updates: dict, state: str, event_type: str, payload: dict | None = None) -> None: ...
    def claim_erp_writeback(self, invoice_id: str, lease_seconds: int = 120) -> bool: ...
    def events(self, invoice_id: str) -> list[dict]: ...
    def get_supplier_fixture(self, supplier_id: str) -> dict | None: ...
    def get_order_fixtures(self, order_ids: tuple[str, ...]) -> list[dict]: ...
    def get_receipt_fixtures(self, receipt_ids: tuple[str, ...]) -> list[dict]: ...
    def list_supplier_invoices(self, supplier_id: str, invoice_number: str) -> list[InvoiceRecord]: ...
    def seed_fixture(self, kind: str, key: str, value: dict) -> None: ...
    def get_fixture(self, kind: str, key: str) -> dict | None: ...


class AccountingConnector(Protocol):
    def get_invoice_evidence(self, invoice: InvoiceRecord) -> AccountingEvidence: ...
    def import_invoice(self, external_id: str) -> tuple[InvoiceRecord, AccountingEvidence]: ...
    def list_receivables(self) -> list[Receivable]:
        """Expected inflows. Defaults to none when the connector has no receivables source."""
        return []

    def list_open_payables(self) -> list[str]:
        """External ids of payables the accounting system still owes.

        Discovery only says what to look at. Whether an invoice may be paid is decided by the
        same policy that governs a hand-imported one, so a connector cannot use this to widen
        the agent's authority. Defaults to none when a connector has no way to enumerate.
        """
        return []
    def find_payment_entry(self, payment_reference: str) -> dict | None: ...
    def create_payment_entry(
        self,
        invoice: InvoiceRecord,
        tx_hash: str,
        mapping: PaymentMapping,
    ) -> ERPWriteResult: ...

    def create_fee_expense(
        self,
        invoice: InvoiceRecord,
        tx_hash: str,
        fee_units: int,
        mapping: PaymentMapping,
    ) -> ERPWriteResult | None:
        """Book the network fee we absorbed as our own expense. ``None`` when there is no fee."""
        ...


class PaymentProvider(Protocol):
    def get_balance(self) -> TreasurySnapshot: ...
    def screen_address(self, address: str | None) -> ScreeningStatus: ...
    def inspect_payment(self, payment: dict) -> PaymentSubmission: ...
    def submit_authorized(
        self,
        payment: dict,
        on_transaction: Callable[[str, str], None] | None = None,
    ) -> PaymentSubmission: ...


class DecisionAgent(Protocol):
    def recommend(self, context: dict) -> AgentRecommendation: ...


class PermitSigner(Protocol):
    address: str
    def sign(self, permit: PaymentPermit) -> str: ...
    def verify(self, permit: PaymentPermit, signature: str) -> bool: ...
    def sign_digest(self, digest: bytes) -> str: ...


class CurrencyConverter(Protocol):
    invoice_currency: str
    settlement_to_invoice_rate: Decimal

    def settlement_amount_usdc(self, amount_units: int, currency: str) -> int | None: ...
    def invoice_units_from_settlement(self, settlement_units: int, invoice_currency: str | None = None) -> int | None: ...
