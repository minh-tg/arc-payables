from __future__ import annotations

import argparse
import hashlib
import uuid
from dataclasses import asdict
from datetime import date, timedelta
from decimal import Decimal

from .domain import InvoiceLine, InvoiceRecord, USDC_SCALE, utcnow
from .settings import get_settings
from .store import SQLiteEvidenceStore

SUPPLIER_ID = "SUP-ACME-001"
APPROVED_WALLET = "0x1111111111111111111111111111111111111111"
ATTACKER_WALLET = "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
SETTLEMENT_CURRENCY = "USDC"


def _units(amount: str) -> int:
    return int(Decimal(amount) * USDC_SCALE)


def _to_erp_units(settlement_units: int, rate: Decimal) -> int:
    converted = Decimal(settlement_units) * rate
    if converted != converted.to_integral_value():
        raise ValueError("seed amount does not convert exactly at the configured rate")
    return int(converted)


def _erp_payable(
    *,
    external_id: str,
    invoice_number: str,
    amount_units: int,
    invoice_currency: str,
    invoice_date: date,
    due_date: date,
    lines: tuple[InvoiceLine, ...],
    purchase_order_ids: tuple[str, ...],
    receipt_ids: tuple[str, ...],
    payment_terms: str | None = None,
    payee_address: str | None = None,
    status: str = "SUBMITTED",
) -> dict:
    """An ERPNext payable as the accounting system holds it: in the accounting currency."""
    return {
        "invoice_id": external_id,
        "invoice_number": invoice_number,
        "supplier_id": SUPPLIER_ID,
        "amount_units": amount_units,
        "currency": invoice_currency.upper(),
        "invoice_date": invoice_date.isoformat(),
        "due_date": due_date.isoformat(),
        "lines": [asdict(line) for line in lines],
        "purchase_order_ids": list(purchase_order_ids),
        "receipt_ids": list(receipt_ids),
        "payment_terms": payment_terms,
        "payee_address": payee_address,
        "status": status,
    }


def _local_invoice(
    payable: dict,
    rate: Decimal,
    *,
    invoice_id: str | None = None,
    payee_address: str | None = None,
    include_payee: bool = True,
) -> InvoiceRecord:
    """The locally captured invoice for an ERP payable, denominated in the settlement asset.

    The ERP payable is the source of truth for amount, dates and lines; the local record only
    differs by the settlement conversion and by holding the (untrusted) captured payee field.
    """
    lines = tuple(
        InvoiceLine(
            item_code=line["item_code"],
            quantity=str(line["quantity"]),
            amount_units=int(Decimal(line["amount_units"]) / rate),
            purchase_order_line_id=line.get("purchase_order_line_id"),
            receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
        )
        for line in payable["lines"]
    )
    return InvoiceRecord(
        id=invoice_id or str(uuid.uuid4()),
        supplier_id=payable["supplier_id"],
        invoice_number=payable["invoice_number"],
        invoice_date=date.fromisoformat(payable["invoice_date"]),
        due_date=date.fromisoformat(payable["due_date"]),
        amount_units=int(Decimal(payable["amount_units"]) / rate),
        currency=SETTLEMENT_CURRENCY,
        invoice_payee_address=payee_address if include_payee else None,
        lines=lines,
        purchase_invoice_id=payable["invoice_id"],
        purchase_order_ids=tuple(payable["purchase_order_ids"]),
        receipt_ids=tuple(payable["receipt_ids"]),
        payment_terms=payable.get("payment_terms"),
        source_text_hash=hashlib.sha256(f"demo capture {payable['invoice_number']}".encode()).hexdigest(),
        source_document_hash=hashlib.sha256(
            f"demo document {payable['invoice_number']} {payable['amount_units']}".encode()
        ).hexdigest(),
    )


def seed_demo(
    store: SQLiteEvidenceStore,
    *,
    invoice_currency: str = "USD",
    settlement_to_invoice_rate: Decimal = Decimal(1),
) -> tuple[str, str]:
    """Seed a simulated ERPNext plus the local captures of two of its payables.

    The accounting system is seeded first and the local invoices are derived from it, so the
    comparison the policy performs is a genuine comparison against a separate record.
    """
    store.initialize()
    rate = Decimal(str(settlement_to_invoice_rate))
    if rate <= 0:
        raise ValueError("settlement-to-invoice rate must be positive")

    store.seed_fixture(
        "supplier",
        SUPPLIER_ID,
        {
            "id": SUPPLIER_ID,
            "name": "Acme Industrial Supply (Demo)",
            "erp_supplier_id": "SUP-ACME-001",
            "approved_wallet": APPROVED_WALLET,
            "wallet_verified": True,
            "wallet_version": "demo-approved-v1",
            "screening": "CLEAR",
        },
    )
    store.seed_fixture("screening", APPROVED_WALLET.lower(), {"status": "CLEAR"})

    po_line_id = "POL-ACME-2026-001-1"
    store.seed_fixture(
        "purchase_order",
        "PO-ACME-2026-001",
        {
            "id": "PO-ACME-2026-001",
            "supplier_id": SUPPLIER_ID,
            "status": "SUBMITTED",
            "lines": [
                {"id": po_line_id, "item_code": "INDUSTRIAL-FILTER", "quantity": "10", "amount_units": _units("250")}
            ],
        },
    )
    store.seed_fixture(
        "receipt",
        "PR-ACME-2026-001",
        {
            "id": "PR-ACME-2026-001",
            "supplier_id": SUPPLIER_ID,
            "status": "SUBMITTED",
            "lines": [
                {"id": "PRL-ACME-2026-001-1", "item_code": "INDUSTRIAL-FILTER", "quantity": "10", "purchase_order_line_id": po_line_id}
            ],
        },
    )

    today = date.today()
    legitimate_payable = _erp_payable(
        external_id="PINV-ACME-2026-001",
        invoice_number="ACME-INV-2026-001",
        amount_units=_to_erp_units(_units("250"), rate),
        invoice_currency=invoice_currency,
        invoice_date=today - timedelta(days=20),
        due_date=today,
        lines=(InvoiceLine("INDUSTRIAL-FILTER", "10", _to_erp_units(_units("250"), rate), po_line_id, ("PRL-ACME-2026-001-1",)),),
        purchase_order_ids=("PO-ACME-2026-001",),
        receipt_ids=("PR-ACME-2026-001",),
        payment_terms="Net 20",
    )
    suspicious_payable = _erp_payable(
        external_id="PINV-ACME-2026-002",
        invoice_number="ACME-INV-2026-002",
        # The accounting system only recognizes a small payable; the captured invoice claims
        # far more and names an attacker wallet as the payee.
        amount_units=_to_erp_units(_units("300"), rate),
        invoice_currency=invoice_currency,
        invoice_date=today - timedelta(days=2),
        due_date=today + timedelta(days=5),
        lines=(InvoiceLine("INDUSTRIAL-FILTER", "10", _to_erp_units(_units("300"), rate), po_line_id, ("PRL-ACME-2026-001-1",)),),
        purchase_order_ids=("PO-ACME-2026-001",),
        receipt_ids=("PR-ACME-2026-001",),
        payment_terms="Net 7",
    )
    importable_payable = _erp_payable(
        external_id="PINV-ACME-2026-003",
        invoice_number="ACME-INV-2026-003",
        amount_units=_to_erp_units(_units("120"), rate),
        invoice_currency=invoice_currency,
        invoice_date=today - timedelta(days=5),
        due_date=today,
        lines=(InvoiceLine("INDUSTRIAL-FILTER", "4.8", _to_erp_units(_units("120"), rate), po_line_id, ("PRL-ACME-2026-001-1",)),),
        purchase_order_ids=("PO-ACME-2026-001",),
        receipt_ids=("PR-ACME-2026-001",),
        payment_terms="Net 5",
    )
    for payable in (legitimate_payable, suspicious_payable, importable_payable):
        store.seed_fixture("erp_payable", payable["invoice_id"], payable)

    legitimate = _local_invoice(
        legitimate_payable, rate, invoice_id="demo-invoice-legitimate", payee_address=APPROVED_WALLET
    )
    # The suspicious capture keeps the attacker payee and a 3200 USDC claim that the ERP
    # payable (300) does not support, so it can never be made payable by approval alone.
    suspicious = _local_invoice(
        suspicious_payable, rate, invoice_id="demo-invoice-suspicious", payee_address=ATTACKER_WALLET
    )
    suspicious = _replace_amount(suspicious, _units("3200"), "80")

    store.create_invoice(legitimate, str(uuid.UUID("11111111-1111-4111-8111-111111111111")))
    store.create_invoice(suspicious, str(uuid.UUID("22222222-2222-4222-8222-222222222222")))
    return legitimate.id, suspicious.id


def _replace_amount(invoice: InvoiceRecord, amount_units: int, quantity: str) -> InvoiceRecord:
    from dataclasses import replace

    line = invoice.lines[0]
    return replace(
        invoice,
        amount_units=amount_units,
        lines=(replace(line, quantity=quantity, amount_units=amount_units),),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed local Arc Payables demo records")
    parser.add_argument("--db", default=None, help="Override local database path")
    args = parser.parse_args()
    settings = get_settings()
    store = SQLiteEvidenceStore(args.db or settings.database_path)
    legitimate_id, suspicious_id = seed_demo(
        store,
        invoice_currency=settings.frappe_invoice_currency or "USD",
        settlement_to_invoice_rate=settings.settlement_to_invoice_rate,
    )
    print(f"Seeded demo invoice {legitimate_id} and suspicious invoice {suspicious_id} (mock data only).")


if __name__ == "__main__":
    main()
