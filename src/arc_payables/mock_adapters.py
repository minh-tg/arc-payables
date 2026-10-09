from __future__ import annotations

import hashlib
import threading
import time
import uuid
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from eth_account import Account

from .accounting import CENT, bookable_fee_amount
from .domain import (
    AccountingEvidence,
    ERPWriteResult,
    InvoiceLine,
    InvoiceRecord,
    PAYABLE_INVOICE_STATUSES,
    PaymentPermit,
    PaymentStatus,
    PaymentSubmission,
    PurchaseOrderEvidence,
    PurchaseOrderLine,
    ReceiptEvidence,
    ReceiptLine,
    ScreeningStatus,
    SupplierRecord,
    TreasurySnapshot,
    USDC_SCALE,
    add_operation_fee,
    is_evm_address,
    utcnow,
)
from .ports import PaymentMapping, PermitSigner, Receivable
from .security import EIP712PermitSigner
from .store import SQLiteEvidenceStore


class MockAccountingConnector:
    def __init__(self, store: SQLiteEvidenceStore):
        self.store = store
        self.entries: dict[str, dict[str, Any]] = {}
        self.fail_next_write = False

class MockErpError(RuntimeError):
    """A mock connector failure that says which kind it is.

    The service reads `status_code` and `uncertain` to decide what to do next, so a test double that
    raises a bare exception is lying about the only thing it was supposed to reproduce. A lost
    response is uncertain: the write may have committed, so the retry must be soon and must
    reconcile. A rejection is definite, so the retry backs off instead of hammering.
    """

    def __init__(self, message: str, *, status_code: int | None = None, uncertain: bool = False):
        super().__init__(message)
        self.status_code = status_code
        self.uncertain = uncertain


class MockAccountingConnector:
    """Simulated ERPNext.

    It models the accounting system the way the real connector does: one ``erp_payable``
    fixture per Purchase Invoice holds the payable in the accounting currency, and importing it
    converts to the settlement currency at the configured rate. A captured invoice is linked to
    such a payable, and stays unpayable while it has none.
    """

    def __init__(
        self,
        store: SQLiteEvidenceStore,
        *,
        invoice_currency: str = "USD",
        settlement_currency: str = "USDC",
        settlement_to_invoice_rate: Decimal = Decimal(1),
    ):
        self.store = store
        self.entries: dict[str, dict[str, Any]] = {}
        self.fee_entries: dict[str, dict[str, Any]] = {}
        # Refused before anything was written: definite, so the retry backs off.
        self.fail_next_write = False
        self.fail_next_fee_write = False
        # Committed, then the response was lost: uncertain, so the claim is kept and the outcome
        # has to be reconciled before anything else is written.
        self.unsure_next_write = False
        self.unsure_next_fee_write = False
        self.invoice_currency = (invoice_currency or "USD").upper()
        self.settlement_currency = (settlement_currency or "USDC").upper()
        self.rate = Decimal(str(settlement_to_invoice_rate))
        if self.rate <= 0:
            raise ValueError("settlement-to-invoice rate must be positive")

    def payable(self, external_id: str | None) -> dict[str, Any] | None:
        """The ERP's own record of a payable, in the accounting currency."""
        if not external_id:
            return None
        return self.store.get_fixture("erp_payable", external_id)

    def list_open_payables(self) -> list[str]:
        """Seeded payables the simulated ERP still owes, in insertion order."""
        return [
            str(payable["invoice_id"])
            for payable in self.store.list_fixtures("erp_payable")
            if str(payable.get("status") or "").upper() in PAYABLE_INVOICE_STATUSES
        ]

    def list_receivables(self) -> list[Receivable]:
        """Seeded sales fixtures: the mock's stand-in for open Sales Invoices."""
        receivables: list[Receivable] = []
        for row in self.store.list_open_receivables():
            try:
                expected = date.fromisoformat(str(row["expected_date"])[:10])
            except ValueError:
                continue
            receivables.append(
                Receivable(
                    external_id=str(row["external_id"]),
                    customer=str(row.get("customer") or ""),
                    reference=str(row.get("reference") or row["external_id"]),
                    amount_units=int(row["amount_units"]),
                    currency=str(row.get("currency") or self.settlement_currency).upper(),
                    expected_date=expected,
                    source=str(row.get("source") or "mock"),
                )
            )
        return receivables

    def _to_settlement_units(self, erp_units: int) -> int:
        settlement = Decimal(erp_units) / self.rate
        if settlement != settlement.to_integral_value():
            raise ValueError(
                "accounting amount does not convert exactly into the settlement currency at the configured rate"
            )
        return int(settlement)

    def get_invoice_evidence(self, invoice: InvoiceRecord) -> AccountingEvidence:
        raw_supplier = self.store.get_supplier_fixture(invoice.supplier_id)
        supplier = None
        if raw_supplier:
            supplier = SupplierRecord(
                id=raw_supplier["id"],
                name=raw_supplier.get("name", raw_supplier["id"]),
                approved_wallet=raw_supplier.get("approved_wallet"),
                wallet_verified=bool(raw_supplier.get("wallet_verified", False)),
                wallet_version=str(raw_supplier.get("wallet_version", "1")),
                screening=ScreeningStatus(raw_supplier.get("screening", "UNAVAILABLE")),
                erp_supplier_id=raw_supplier.get("erp_supplier_id", raw_supplier["id"]),
                payment_blocked=bool(raw_supplier.get("payment_blocked", False)),
                blocked_reason=raw_supplier.get("blocked_reason"),
            )
        orders = tuple(self._order(item) for item in self.store.get_order_fixtures(invoice.purchase_order_ids))
        receipts = tuple(self._receipt(item) for item in self.store.get_receipt_fixtures(invoice.receipt_ids))
        duplicate = self.store.get_fixture("duplicate_invoice", f"{invoice.supplier_id}:{invoice.invoice_number}")
        status = self.store.get_fixture("invoice_status", invoice.id)
        # The payable is a distinct record, not a copy of the invoice under test. An invoice
        # that claims an ERP link the accounting system cannot produce stays unlinked rather
        # than silently validating itself.
        payable = self.payable(invoice.purchase_invoice_id)
        source_lines = (
            tuple(InvoiceLine(**{**line, "receipt_line_ids": tuple(line.get("receipt_line_ids", ()))})
                  for line in payable.get("lines", []))
            if payable else ()
        )
        return AccountingEvidence(
            supplier=supplier,
            purchase_orders=orders,
            receipts=receipts,
            duplicate_invoice_id=duplicate.get("invoice_id") if duplicate else None,
            invoice_status=(str(payable.get("status", "SUBMITTED")) if payable else "UNVERIFIED")
            if not status else str(status.get("status", "SUBMITTED")),
            screening=supplier.screening if supplier else ScreeningStatus.UNAVAILABLE,
            invoice_linked=payable is not None,
            source_invoice_amount_units=int(payable["amount_units"]) if payable else None,
            source_invoice_currency=str(payable["currency"]).upper() if payable else None,
            source_invoice_number=str(payable["invoice_number"]) if payable else None,
            source_supplier_id=str(payable["supplier_id"]) if payable else None,
            source_invoice_id=str(payable["invoice_id"]) if payable else None,
            source_invoice_lines=source_lines,
        )

    def import_invoice(self, external_id: str) -> tuple[InvoiceRecord, AccountingEvidence]:
        payable = self.payable(external_id)
        if not payable:
            raise LookupError("Accounting invoice was not found")
        currency = str(payable["currency"]).upper()
        if currency != self.invoice_currency:
            raise ValueError(
                f"accounting document currency {currency or 'missing'} is not the configured invoice currency {self.invoice_currency}"
            )
        lines = tuple(
            InvoiceLine(
                item_code=str(line["item_code"]),
                quantity=str(line["quantity"]),
                amount_units=self._to_settlement_units(int(line["amount_units"])),
                purchase_order_line_id=line.get("purchase_order_line_id"),
                receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
            )
            for line in payable.get("lines", [])
        )
        invoice = InvoiceRecord(
            id=str(uuid.uuid4()),
            supplier_id=str(payable["supplier_id"]),
            invoice_number=str(payable["invoice_number"]),
            invoice_date=date.fromisoformat(payable["invoice_date"]),
            due_date=date.fromisoformat(payable["due_date"]),
            amount_units=self._to_settlement_units(int(payable["amount_units"])),
            currency=self.settlement_currency,
            invoice_payee_address=payable.get("payee_address"),
            lines=lines,
            purchase_invoice_id=external_id,
            purchase_order_ids=tuple(payable.get("purchase_order_ids", ())),
            receipt_ids=tuple(payable.get("receipt_ids", ())),
            payment_terms=payable.get("payment_terms"),
            source_document_hash=None,
        )
        if lines and sum(line.amount_units for line in lines) != invoice.amount_units:
            raise ValueError("converted accounting lines do not sum to the converted payable total")
        return invoice, self.get_invoice_evidence(invoice)

    def find_payment_entry(self, payment_reference: str) -> dict | None:
        return self.entries.get(payment_reference)

    def create_payment_entry(
        self,
        invoice: InvoiceRecord,
        tx_hash: str,
        mapping: PaymentMapping,
    ) -> ERPWriteResult:
        if self.fail_next_write:
            self.fail_next_write = False
            raise MockErpError("mock ERPNext rejected the Payment Entry", status_code=417)
        existing = self.entries.get(tx_hash)
        if existing:
            if existing["invoice_id"] != invoice.id:
                raise ValueError("ERP reference already belongs to a different invoice")
            existing["docstatus"] = 1
            return ERPWriteResult(existing["name"], 1, already_existed=True)
        entry_id = f"PE-MOCK-{len(self.entries) + 1:05d}"
        self.entries[tx_hash] = {
            "name": entry_id,
            "invoice_id": invoice.id,
            "docstatus": 1,
            "mapping": mapping.__dict__,
        }
        if self.unsure_next_write:
            self.unsure_next_write = False
            raise MockErpError("mock ERPNext lost the response after its idempotent write committed", uncertain=True)
        return ERPWriteResult(entry_id, 1, already_existed=False)

    def create_fee_expense(
        self,
        invoice: InvoiceRecord,
        tx_hash: str,
        fee_units: int,
        mapping: PaymentMapping,
    ) -> ERPWriteResult | None:
        """Book the network fee as its own expense entry, exactly as the live connector does."""
        if fee_units <= 0:
            return None
        if self.fail_next_fee_write:
            self.fail_next_fee_write = False
            raise MockErpError("mock ERPNext rejected the network-fee entry", status_code=417)
        existing = self.fee_entries.get(tx_hash)
        if existing:
            if existing["invoice_id"] != invoice.id:
                raise ValueError("fee reference already belongs to a different invoice")
            existing["docstatus"] = 1
            return ERPWriteResult(existing["name"], 1, already_existed=True)
        entry_id = f"JV-MOCK-{len(self.fee_entries) + 1:05d}"
        self.fee_entries[tx_hash] = {
            "name": entry_id,
            "invoice_id": invoice.id,
            "docstatus": 1,
            "fee_units": fee_units,
            "fee_amount": bookable_fee_amount(fee_units, smallest_unit=CENT),
            "account": mapping.fee_account,
            "settlement_account": mapping.paid_from,
            "cost_center": mapping.cost_center,
        }
        if self.unsure_next_fee_write:
            self.unsure_next_fee_write = False
            raise MockErpError("mock ERPNext lost the response after its idempotent fee write committed", uncertain=True)
        return ERPWriteResult(entry_id, 1, already_existed=False)

    @staticmethod
    def _order(raw: dict[str, Any]) -> PurchaseOrderEvidence:
        return PurchaseOrderEvidence(
            id=raw["id"],
            supplier_id=raw["supplier_id"],
            status=raw.get("status", "SUBMITTED"),
            lines=tuple(PurchaseOrderLine(**line) for line in raw.get("lines", [])),
        )

    @staticmethod
    def _receipt(raw: dict[str, Any]) -> ReceiptEvidence:
        return ReceiptEvidence(
            id=raw["id"],
            supplier_id=raw["supplier_id"],
            status=raw.get("status", "SUBMITTED"),
            lines=tuple(ReceiptLine(**line) for line in raw.get("lines", [])),
        )


class MockPaymentProvider:
    """Mocked provider exercises the same permit, idempotency and reconciliation interface."""

    def __init__(
        self,
        store: SQLiteEvidenceStore,
        signer: PermitSigner | None = None,
        wallet_address: str = "0x0000000000000000000000000000000000000001",
        guard_address: str = "0x0000000000000000000000000000000000000002",
        failure_mode: str | None = None,
        balance_units: int = 5_000 * USDC_SCALE,
        fee_units: int = 0,
        deferred_fee: bool = False,
        approve_fee_units: int = 0,
    ):
        self.store = store
        self.signer = signer or EIP712PermitSigner(Account.create().key)
        self.wallet_address = wallet_address
        self.guard_address = guard_address
        self.failure_mode = failure_mode
        self._balance_units = balance_units
        # Models the allowance operation that precedes the guarded call on a real chain, so the
        # accounting path for a multi-operation fee can be tested without one.
        self.approve_fee_units = approve_fee_units
        self._payments_by_id: dict[str, PaymentSubmission] = {}
        self._payments_by_key: dict[str, PaymentSubmission] = {}
        self._lock = threading.Lock()
        self.submission_calls = 0
        self.fee_units = fee_units
        # A real provider often cannot name the network fee while the transfer is still being
        # indexed. Deferred models that: the settlement reports no fee, and a later ask reports it.
        self.deferred_fee = deferred_fee
        self._guard_paused = False

    def set_guard_paused(self, paused: bool) -> str:
        self._guard_paused = bool(paused)
        return "0x" + hashlib.sha256(f"mock-pause:{paused}".encode()).hexdigest()

    def get_balance(self) -> TreasurySnapshot:
        return TreasurySnapshot(self._balance_units, utcnow(), "treasury:mock:wallet-balance", "mock_arc_wallet_balance")

    def screen_address(self, address: str | None) -> ScreeningStatus:
        if not address:
            return ScreeningStatus.UNAVAILABLE
        fixture = self.store.get_fixture("screening", address.lower())
        return ScreeningStatus(fixture["status"]) if fixture else ScreeningStatus.UNAVAILABLE

    def inspect_payment(self, payment: dict) -> PaymentSubmission:
        payment_id = payment["payment_id"]
        known = self._payments_by_id.get(payment_id)
        if known:
            # The fee comes back once the transaction is indexed, which is the whole reason a
            # writeback asks the provider again instead of accepting the blank it was given.
            # What the provider could not name is the *guarded call's* fee; the allowance operation
            # that preceded it was already measurable, so an inspection reports that stage alone and
            # the caller merges it with what it already had.
            if not self.deferred_fee:
                breakdown = dict(known.fee_breakdown or {})
                breakdown["guard"] = self.fee_units
                return replace(known, fee_units=self.fee_units, fee_breakdown=breakdown)
            return known
        if self.failure_mode == "uncertain" and payment_id in self._payments_by_id:
            return PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="MOCK_UNCERTAIN")
        return PaymentSubmission(PaymentStatus.NOT_FOUND)

    def submit_authorized(self, payment: dict, on_transaction=None) -> PaymentSubmission:
        with self._lock:
            self.submission_calls += 1
            return self._submit_authorized_locked(payment, on_transaction)

    def _submit_authorized_locked(self, payment: dict, on_transaction=None) -> PaymentSubmission:
        payment_id = payment["payment_id"]
        idempotency_key = payment["idempotency_key"]
        if idempotency_key in self._payments_by_key:
            return self._payments_by_key[idempotency_key]
        known = self._payments_by_id.get(payment_id)
        if known:
            return known
        permit = PaymentPermit(**payment["permit"])
        signature = payment["signature"]
        if not self.signer.verify(permit, signature):
            raise ValueError("Payment permit signature is invalid")
        if (
            permit.payer.lower() != self.wallet_address.lower()
            or permit.guard_address.lower() != self.guard_address.lower()
            or permit.chain_id != 5_042_002
            or permit.token.lower() != "0x3600000000000000000000000000000000000000"
            or permit.recipient.lower() == permit.payer.lower()
            or not is_evm_address(permit.recipient)
        ):
            raise ValueError("Payment permit domain or fields are invalid")
        if permit.expiry <= int(time.time()):
            raise ValueError("Payment permit is expired")
        if self._guard_paused:
            result = PaymentSubmission(PaymentStatus.FAILED, failure_code="GUARD_PAUSED")
            self._payments_by_id[payment_id] = result
            self._payments_by_key[idempotency_key] = result
            return result
        if self.failure_mode == "insufficient_balance" or self._balance_units < permit.amount_units:
            result = PaymentSubmission(PaymentStatus.FAILED, failure_code="INSUFFICIENT_BALANCE")
            self._payments_by_id[payment_id] = result
            self._payments_by_key[idempotency_key] = result
            return result
        if self.failure_mode == "reverted":
            # The guarded call reverts after the allowance was already set, so its gas is spent and
            # must still be recorded: a failed payment is not a free one.
            spent = {"approve": self.approve_fee_units} if self.approve_fee_units else {}
            result = PaymentSubmission(
                PaymentStatus.FAILED,
                failure_code="CONTRACT_REVERTED",
                fee_units=sum(spent.values()) or None,
                fee_breakdown=spent or None,
            )
            self._payments_by_id[payment_id] = result
            self._payments_by_key[idempotency_key] = result
            return result
        if self.failure_mode == "rpc_timeout":
            # The mock has no proof that broadcast did not happen: require reconciliation.
            result = PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="RPC_TIMEOUT")
            self._payments_by_id[payment_id] = result
            return result
        if self.failure_mode == "uncertain":
            # Simulates on-chain use with a lost provider response/hash.
            self._balance_units -= permit.amount_units
            result = PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="RESULT_UNKNOWN")
            self._payments_by_id[payment_id] = result
            return result
        fees: dict[str, int] = {}
        if self.approve_fee_units:
            if on_transaction:
                on_transaction("approve", f"mock-approve-{idempotency_key}")
            add_operation_fee(fees, "approve", self.approve_fee_units)
        if on_transaction:
            on_transaction("contract_execution", f"mock-{idempotency_key}")
        add_operation_fee(fees, "guard", None if self.deferred_fee else self.fee_units)
        self._balance_units -= permit.amount_units
        # A distinct transaction hash. Deriving it from the payment id made the two fields identical
        # in the double, so a lookup by hash and a lookup by payment id were indistinguishable and a
        # bug in either would have passed. A real provider returns a hash of the signed transaction,
        # which is not the id the guard consumes.
        transaction_hash = "0x" + hashlib.sha256(f"mock-tx:{payment_id}".encode()).hexdigest()
        result = PaymentSubmission(
            PaymentStatus.CONFIRMED,
            transaction_hash=transaction_hash,
            provider_transaction_id=f"mock-{idempotency_key}",
            fee_units=sum(fees.values()) if fees else None,
            fee_breakdown=dict(fees) or None,
        )
        self._payments_by_id[payment_id] = result
        self._payments_by_key[idempotency_key] = result
        return result
