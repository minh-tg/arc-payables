"""Read-only check of what ERPNext holds for one settled Arc transaction.

A writeback that reports success is the application's belief. This reads the accounting system
itself and answers, for one transaction hash, whether the Payment Entry, the Purchase Invoice,
the network-fee journal and the ledger rows agree with each other. It creates, submits and
cancels nothing, and it goes through the same connector as the writeback, so Administrator
credentials are refused here too.

Each check is ``ok``, ``fail`` or ``skipped``. A check is skipped, never passed, when the
runtime user is not allowed to read what it needs (typically the General Ledger), so a
least-privilege account produces a smaller report instead of a false one.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from .frappe_adapter import FrappeAccountingConnector, FrappeAdapterError
from .settings import get_settings

OK, FAIL, SKIPPED = "ok", "fail", "skipped"


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class Report:
    tx_hash: str
    checks: list[Check] = field(default_factory=list)
    payment_entry: str | None = None
    purchase_invoice: str | None = None
    fee_entry: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.status != FAIL for check in self.checks)

    def add(self, name: str, passed: bool, detail: str) -> bool:
        self.checks.append(Check(name, OK if passed else FAIL, detail))
        return passed

    def skip(self, name: str, detail: str) -> None:
        self.checks.append(Check(name, SKIPPED, detail))


def _number(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _ledger_balance(connector: FrappeAccountingConnector, voucher: str) -> tuple[Decimal, Decimal, int]:
    rows = connector.list_documents(
        "GL Entry",
        [["voucher_no", "=", voucher], ["is_cancelled", "=", 0]],
        ["account", "debit", "credit"],
        limit=100,
    )
    debit = sum((_number(row.get("debit")) or Decimal(0) for row in rows), Decimal(0))
    credit = sum((_number(row.get("credit")) or Decimal(0) for row in rows), Decimal(0))
    return debit, credit, len(rows)


def _check_ledger(report: Report, connector: FrappeAccountingConnector, label: str, voucher: str) -> None:
    name = f"{label}_ledger_balanced"
    try:
        debit, credit, rows = _ledger_balance(connector, voucher)
    except FrappeAdapterError as exc:
        if exc.status_code in {401, 403}:
            report.skip(name, "the runtime user cannot read GL Entry; not checked")
            return
        report.add(name, False, f"could not read the ledger rows: {exc}")
        return
    report.add(
        name,
        rows > 0 and debit == credit,
        f"{rows} uncancelled rows, debit {debit} and credit {credit}",
    )


def verify_settlement(
    connector: FrappeAccountingConnector,
    tx_hash: str,
    *,
    invoice: str | None = None,
    allow_missing_fee: bool = False,
) -> Report:
    report = Report(tx_hash=tx_hash)
    try:
        entry = connector.find_payment_entry(connector.payment_reference(tx_hash))
    except FrappeAdapterError as exc:
        report.add("payment_entry_found", False, str(exc))
        return report
    if not report.add("payment_entry_found", entry is not None, "no Payment Entry carries this transaction hash"
                      if entry is None else f"{entry.get('name')}"):
        return report
    assert entry is not None
    report.payment_entry = str(entry.get("name"))
    report.add("payment_entry_submitted", int(entry.get("docstatus") or 0) == 1, f"docstatus {entry.get('docstatus')}")
    report.add(
        "payment_entry_pays_a_supplier",
        entry.get("payment_type") == "Pay" and entry.get("party_type") == "Supplier",
        f"{entry.get('payment_type')} / {entry.get('party_type')}",
    )
    refs = [ref for ref in entry.get("references") or [] if ref.get("reference_doctype") == "Purchase Invoice"]
    allocated = sum((_number(ref.get("allocated_amount")) or Decimal(0) for ref in refs), Decimal(0))
    paid, received = _number(entry.get("paid_amount")), _number(entry.get("received_amount"))
    report.add(
        "payment_entry_amounts_agree",
        paid is not None and paid > 0 and paid == received == allocated and not entry.get("deductions"),
        f"paid {paid}, received {received}, allocated {allocated}, deductions {len(entry.get('deductions') or [])}",
    )
    linked = report.add("payment_entry_references_one_invoice", len(refs) == 1, f"{len(refs)} Purchase Invoice reference(s)")
    if linked:
        report.purchase_invoice = str(refs[0].get("reference_name"))
        if invoice:
            report.add(
                "payment_entry_matches_requested_invoice",
                report.purchase_invoice == invoice,
                f"allocated to {report.purchase_invoice}, expected {invoice}",
            )
        try:
            raw = connector.get_document("Purchase Invoice", report.purchase_invoice)
        except FrappeAdapterError as exc:
            report.add("invoice_settled", False, f"could not read the Purchase Invoice: {exc}")
        else:
            outstanding = _number(raw.get("outstanding_amount"))
            report.add(
                "invoice_settled",
                int(raw.get("docstatus") or 0) == 1 and outstanding == 0,
                f"docstatus {raw.get('docstatus')}, status {raw.get('status')}, outstanding {outstanding}",
            )
    _check_ledger(report, connector, "payment_entry", report.payment_entry)

    try:
        fee = connector.find_fee_expense(connector.fee_reference(tx_hash))
    except FrappeAdapterError as exc:
        report.add("fee_entry_found", False, str(exc))
        return report
    if fee is None:
        if allow_missing_fee:
            report.skip("fee_entry_found", "no fee journal, accepted by request")
        else:
            report.add("fee_entry_found", False, "no network-fee Journal Entry carries this transaction hash")
        return report
    report.fee_entry = str(fee.get("name"))
    report.add("fee_entry_found", True, report.fee_entry)
    debit, credit = _number(fee.get("total_debit")), _number(fee.get("total_credit"))
    report.add(
        "fee_entry_submitted_and_balanced",
        int(fee.get("docstatus") or 0) == 1 and debit is not None and debit > 0 and debit == credit,
        f"docstatus {fee.get('docstatus')}, debit {debit}, credit {credit}",
    )
    _check_ledger(report, connector, "fee_entry", report.fee_entry)
    return report


def render(report: Report) -> str:
    marks = {OK: "OK  ", FAIL: "FAIL", SKIPPED: "SKIP"}
    lines = [f"ERPNext documents for {report.tx_hash}"]
    lines += [f"  {marks[check.status]}  {check.name}: {check.detail}" for check in report.checks]
    lines.append("RESULT: " + ("consistent" if report.ok else "NOT consistent"))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="arc-payables-erp-verify",
        description="Read-only check of the ERPNext documents for one settled Arc transaction.",
    )
    parser.add_argument("--tx", required=True, help="Arc transaction hash the writeback was keyed to")
    parser.add_argument("--invoice", help="Expected Purchase Invoice name")
    parser.add_argument("--allow-missing-fee", action="store_true", help="Accept a payment with no fee journal")
    parser.add_argument("--json", action="store_true", help="Emit the report as JSON")
    args = parser.parse_args(argv)

    connector = FrappeAccountingConnector(get_settings())
    if not connector.configured:
        print("FRAPPE_URL, FRAPPE_API_KEY and FRAPPE_API_SECRET must be set.", file=sys.stderr)
        return 2
    try:
        report = verify_settlement(
            connector, args.tx, invoice=args.invoice, allow_missing_fee=args.allow_missing_fee
        )
    except FrappeAdapterError as exc:
        print(f"Could not read ERPNext: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({**asdict(report), "ok": report.ok}, indent=2) if args.json else render(report))
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
