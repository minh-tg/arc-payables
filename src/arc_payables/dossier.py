"""Export and verify one settled payment as a self-contained JSON dossier.

The bundle carries the evidence inputs the decision read, the signed permit, the recorded
settlement and the audit chain up to the invoice's last entry. The verifier is offline on
purpose: it recomputes the evidence hash, recovers the permit signer from the signature and
walks the included hash links. It cannot prove that the signer was authorised, that the chain
transaction exists, that the ERP documents exist, or that no audit entry was left out, and it
says so on every run.

    arc-payables-dossier export <invoice> [--out FILE] [--full-hashes]
    arc-payables-dossier verify <file> [--full-hashes]

Exit codes: 0 verified, 1 a check failed, 2 refused (nothing checked or written).
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eth_account import Account

from .domain import (
    AccountingEvidence,
    InvoiceLine,
    InvoiceRecord,
    PaymentPermit,
    PurchaseOrderEvidence,
    PurchaseOrderLine,
    ReceiptEvidence,
    ReceiptLine,
    ScreeningStatus,
    SupplierRecord,
    TreasurySnapshot,
    USDC_SCALE,
)
from .policy import compute_evidence_hash
from .screening import ScreeningMatch, ScreeningResult
from .security import permit_typed_message
from .store import GENESIS_HASH, SQLiteEvidenceStore, canonical_json
from .verify_arc import short_hash

SCHEMA_VERSION = 1
KIND = "arc-payables-payment-dossier"
TOTAL_CHECKS = 3

NOT_CHECKED = (
    "that the named signer may sign permits",
    "that transaction {transaction} exists or is final",
    "that the ERP documents exist or match their references",
    "that the file holds every audit entry for this invoice",
)
NOT_CHECKED_SIMULATED = "that the payment is real: it is simulated, no transaction exists"
NOT_CHECKED_SHORT = (
    "signer authority",
    "the chain transaction",
    "the ERP documents",
    "whether the file holds every audit entry",
)


class DossierRefusal(Exception):
    """A reason the export or verify refused to run. Nothing is written or checked."""

    def __init__(self, headline: str, reason: str, *, reference: str | None = None):
        super().__init__(headline)
        self.headline = headline
        self.reason = reason
        self.reference = reference


@dataclass
class VerifyOutcome:
    verdict: str
    exit_code: int
    lines: list[str] = field(default_factory=list)


def _line(label: str, value: str) -> str:
    return f"{label + ':':<20}{value}"


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")


# --------------------------------------------------------------------------------------
# Evidence reconstruction
#
# The decision hashed raw domain objects, and the store keeps only a display snapshot. These
# rebuild the raw inputs from that snapshot plus the supplier fixture, so the recomputed hash
# can be compared with the one the permit signed.
# --------------------------------------------------------------------------------------


def _supplier_from_snapshot(snapshot: dict, fixture: dict) -> SupplierRecord:
    supplier = snapshot["supplier"]
    return SupplierRecord(
        id=supplier["id"],
        name=supplier["name"],
        approved_wallet=supplier["approved_wallet"],
        wallet_verified=bool(supplier["wallet_verified"]),
        wallet_version=supplier["wallet_version"],
        screening=ScreeningStatus(fixture.get("screening") or ScreeningStatus.UNAVAILABLE.value),
        erp_supplier_id=fixture.get("erp_supplier_id"),
        payment_blocked=bool(fixture.get("payment_blocked")),
        blocked_reason=fixture.get("blocked_reason"),
    )


def _screening_from_snapshot(snapshot: dict) -> ScreeningResult:
    screening = snapshot["screening"]
    return ScreeningResult(
        status=ScreeningStatus(screening["status"]),
        provider=screening["provider"],
        subject=screening.get("subject") or "",
        checked_at=datetime.fromisoformat(screening["checked_at"]),
        dataset=screening.get("dataset"),
        wallet=screening.get("wallet"),
        matches=tuple(ScreeningMatch(**match) for match in screening.get("matches", ())),
        reason=screening.get("reason") or "",
        response_hash=screening.get("response_hash"),
    )


def _accounting_from_snapshot(snapshot: dict, fixture: dict) -> AccountingEvidence:
    source = snapshot["accounting_source_invoice"]
    return AccountingEvidence(
        supplier=_supplier_from_snapshot(snapshot, fixture),
        purchase_orders=tuple(
            PurchaseOrderEvidence(
                id=order["id"],
                supplier_id=order["supplier_id"],
                status=order["status"],
                lines=tuple(PurchaseOrderLine(**line) for line in order["lines"]),
            )
            for order in snapshot["purchase_orders"]
        ),
        receipts=tuple(
            ReceiptEvidence(
                id=receipt["id"],
                supplier_id=receipt["supplier_id"],
                status=receipt["status"],
                lines=tuple(ReceiptLine(**line) for line in receipt["lines"]),
            )
            for receipt in snapshot["receipts"]
        ),
        duplicate_invoice_id=snapshot["duplicate_invoice_id"],
        invoice_status=snapshot["invoice_status"],
        invoice_linked=True,
        source_invoice_amount_units=source["amount_units"],
        source_invoice_currency=source["currency"],
        source_invoice_number=source["number"],
        source_supplier_id=source["supplier_id"],
        source_invoice_id=source["id"],
        source_invoice_lines=tuple(
            InvoiceLine(
                item_code=line["item_code"],
                quantity=str(line["quantity"]),
                amount_units=line["amount_units"],
                purchase_order_line_id=line.get("purchase_order_line_id"),
                receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
            )
            for line in source["lines"]
        ),
    )


def _treasury_from_snapshot(snapshot: dict) -> TreasurySnapshot:
    treasury = snapshot["treasury"]
    return TreasurySnapshot(
        balance_units=treasury["balance_units"],
        captured_at=datetime.fromisoformat(treasury["captured_at"]),
        evidence_id=treasury["evidence_id"],
        source=treasury["source"],
    )


def _evidence_payload(invoice: InvoiceRecord, snapshot: dict, fixture: dict) -> dict[str, Any]:
    accounting = _accounting_from_snapshot(snapshot, fixture)
    treasury = _treasury_from_snapshot(snapshot)
    screening = _screening_from_snapshot(snapshot)
    return {
        "hash": compute_evidence_hash(invoice, accounting, treasury, screening),
        "invoice": asdict(invoice),
        "accounting": {
            "supplier": asdict(accounting.supplier) if accounting.supplier else None,
            "purchase_orders": [asdict(order) for order in accounting.purchase_orders],
            "receipts": [asdict(receipt) for receipt in accounting.receipts],
            "duplicate_invoice_id": accounting.duplicate_invoice_id,
            "invoice_status": accounting.invoice_status,
            "source_invoice": {
                "amount_units": accounting.source_invoice_amount_units,
                "currency": accounting.source_invoice_currency,
                "number": accounting.source_invoice_number,
                "supplier_id": accounting.source_supplier_id,
                "id": accounting.source_invoice_id,
                "lines": [asdict(line) for line in accounting.source_invoice_lines],
            },
        },
        "treasury": asdict(treasury),
        "screening": screening.to_dict(),
    }


def _evidence_objects(evidence: dict) -> tuple[InvoiceRecord, AccountingEvidence, TreasurySnapshot, ScreeningResult]:
    raw = evidence["invoice"]
    invoice = InvoiceRecord(
        id=raw["id"],
        supplier_id=raw["supplier_id"],
        invoice_number=raw["invoice_number"],
        invoice_date=date.fromisoformat(raw["invoice_date"]),
        due_date=date.fromisoformat(raw["due_date"]),
        amount_units=int(raw["amount_units"]),
        currency=raw["currency"],
        invoice_payee_address=raw["invoice_payee_address"],
        lines=tuple(
            InvoiceLine(
                item_code=line["item_code"],
                quantity=str(line["quantity"]),
                amount_units=int(line["amount_units"]),
                purchase_order_line_id=line.get("purchase_order_line_id"),
                receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
            )
            for line in raw.get("lines", ())
        ),
        purchase_invoice_id=raw.get("purchase_invoice_id"),
        purchase_order_ids=tuple(raw.get("purchase_order_ids", ())),
        receipt_ids=tuple(raw.get("receipt_ids", ())),
        discount_percent=raw.get("discount_percent"),
        discount_deadline=date.fromisoformat(raw["discount_deadline"]) if raw.get("discount_deadline") else None,
        payment_terms=raw.get("payment_terms"),
        source_text_hash=raw.get("source_text_hash"),
        source_document_hash=raw.get("source_document_hash"),
        source_text=raw.get("source_text"),
    )
    source = evidence["accounting"]["source_invoice"]
    accounting = AccountingEvidence(
        supplier=SupplierRecord(**evidence["accounting"]["supplier"]),
        purchase_orders=tuple(
            PurchaseOrderEvidence(
                id=order["id"],
                supplier_id=order["supplier_id"],
                status=order["status"],
                lines=tuple(PurchaseOrderLine(**line) for line in order["lines"]),
            )
            for order in evidence["accounting"]["purchase_orders"]
        ),
        receipts=tuple(
            ReceiptEvidence(
                id=receipt["id"],
                supplier_id=receipt["supplier_id"],
                status=receipt["status"],
                lines=tuple(ReceiptLine(**line) for line in receipt["lines"]),
            )
            for receipt in evidence["accounting"]["receipts"]
        ),
        duplicate_invoice_id=evidence["accounting"]["duplicate_invoice_id"],
        invoice_status=evidence["accounting"]["invoice_status"],
        invoice_linked=True,
        source_invoice_amount_units=source["amount_units"],
        source_invoice_currency=source["currency"],
        source_invoice_number=source["number"],
        source_supplier_id=source["supplier_id"],
        source_invoice_id=source["id"],
        source_invoice_lines=tuple(
            InvoiceLine(
                item_code=line["item_code"],
                quantity=str(line["quantity"]),
                amount_units=int(line["amount_units"]),
                purchase_order_line_id=line.get("purchase_order_line_id"),
                receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
            )
            for line in source["lines"]
        ),
    )
    treasury = TreasurySnapshot(
        balance_units=int(evidence["treasury"]["balance_units"]),
        captured_at=datetime.fromisoformat(evidence["treasury"]["captured_at"]),
        evidence_id=evidence["treasury"]["evidence_id"],
        source=evidence["treasury"]["source"],
    )
    screening_raw = evidence["screening"]
    screening = ScreeningResult(
        status=ScreeningStatus(screening_raw["status"]),
        provider=screening_raw["provider"],
        subject=screening_raw.get("subject") or "",
        checked_at=datetime.fromisoformat(screening_raw["checked_at"]),
        dataset=screening_raw.get("dataset"),
        wallet=screening_raw.get("wallet"),
        matches=tuple(ScreeningMatch(**match) for match in screening_raw.get("matches", ())),
        reason=screening_raw.get("reason") or "",
        response_hash=screening_raw.get("response_hash"),
    )
    return invoice, accounting, treasury, screening


# --------------------------------------------------------------------------------------
# Building a bundle
# --------------------------------------------------------------------------------------


def build_document(store: SQLiteEvidenceStore, settings: Any, invoice: InvoiceRecord) -> dict[str, Any]:
    """The dossier for one recorded settlement, or a DossierRefusal explaining why not."""
    reference = invoice.invoice_number
    payment = store.get_payment(invoice.id)
    if payment is None:
        raise DossierRefusal(
            "no settled payment is recorded for this invoice",
            "no payment record exists for this invoice",
            reference=reference,
        )
    if payment.get("confirmation_status") != "CONFIRMED":
        raise DossierRefusal(
            "no settled payment is recorded for this invoice",
            f"payment state is {payment.get('state')}; export needs a recorded settlement",
            reference=reference,
        )
    snapshot = store.get_evidence_snapshot(invoice.id)
    if not snapshot:
        raise DossierRefusal("the evidence snapshot is missing", "evidence snapshot missing", reference=reference)
    permit_raw = payment.get("permit")
    signature = payment.get("signature")
    if not permit_raw or not signature:
        raise DossierRefusal("the signed permit is missing", "permit missing", reference=reference)
    entries = _audit_prefix(store, invoice.id, reference)
    fixture = store.get_supplier_fixture(invoice.supplier_id) or {}
    evidence = _evidence_payload(invoice, snapshot, fixture)
    if evidence["hash"] != permit_raw.get("evidence_hash"):
        raise DossierRefusal(
            "the stored evidence does not reproduce the signed hash",
            "evidence hash differs from the permit; export needs the evidence the permit signed",
            reference=reference,
        )
    signer = _recovered_signer(permit_raw, signature)
    if signer is None:
        raise DossierRefusal(
            "the permit signature does not recover a signer",
            "permit signature is unusable",
            reference=reference,
        )
    signer = signer.lower()
    simulated = str(getattr(settings, "payment_provider", "") or "").lower() == "mock"
    return {
        "kind": KIND,
        "schema": SCHEMA_VERSION,
        "invoice": {
            "id": invoice.id,
            "number": invoice.invoice_number,
            "supplier_id": invoice.supplier_id,
            "amount_usdc": _usdc(invoice.amount_units),
            "currency": invoice.currency.upper(),
            "state": store.get_state(invoice.id),
        },
        "payment": {
            "payment_id": payment.get("payment_id"),
            "state": payment.get("state"),
            "confirmation_status": payment.get("confirmation_status"),
            "simulated": simulated,
            "network": None if simulated else "Arc Testnet",
            "transaction_hash": payment.get("transaction_hash"),
            "settled_at": payment.get("settled_at"),
            "permit": permit_raw,
            "signature": signature,
            "signer": signer,
            "erp": {
                "payment_entry_id": payment.get("erp_entry_id"),
                "fee_entry_id": payment.get("erp_fee_entry_id"),
                "purchase_invoice_id": invoice.purchase_invoice_id,
            },
        },
        "evidence": evidence,
        "audit": {"entries": entries},
    }


def _recovered_signer(permit_raw: dict, signature: str) -> str | None:
    try:
        return Account.recover_message(permit_typed_message(PaymentPermit(**permit_raw)), signature=signature)
    except (ValueError, TypeError):
        return None


def _audit_prefix(store: SQLiteEvidenceStore, invoice_id: str, reference: str) -> list[dict[str, Any]]:
    """The hash-linked chain through this invoice's last entry, in id order.

    The invoice's entries share the global chain with other invoices, so the entries between
    them are included too. Without them a previous-hash link cannot be checked offline.
    """
    with store._connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id,invoice_id,event_type,state,payload_json,created_at,prev_hash,event_hash "
                "FROM audit_events WHERE event_hash IS NOT NULL ORDER BY id"
            ).fetchall()
        ]
    own = [row["id"] for row in rows if row["invoice_id"] == invoice_id]
    if not own:
        raise DossierRefusal("no audit entries are recorded for this invoice", "audit entries missing", reference=reference)
    last = max(own)
    entries = []
    for row in rows:
        if row["id"] > last:
            break
        payload = json.loads(row["payload_json"])
        if canonical_json(payload) != row["payload_json"]:
            raise DossierRefusal(
                "an audit entry is not stored in canonical form",
                "audit entry payload cannot be reproduced exactly",
                reference=reference,
            )
        entries.append(
            {
                "id": row["id"],
                "invoice_id": row["invoice_id"],
                "type": row["event_type"],
                "state": row["state"],
                "payload": payload,
                "created_at": row["created_at"],
                "prev_hash": row["prev_hash"],
                "event_hash": row["event_hash"],
            }
        )
    return entries


# --------------------------------------------------------------------------------------
# Verifying a bundle
# --------------------------------------------------------------------------------------


def verify_document(document: Any, reference: str, *, full_hashes: bool = False) -> VerifyOutcome:
    """Re-run the three offline checks over a parsed bundle."""
    _validate(document, reference)
    evidence = document["evidence"]
    payment = document["payment"]
    permit = payment["permit"]
    entries = document["audit"]["entries"]

    evidence_ok, recomputed, permit_hash = _check_evidence(evidence, permit)
    signature_ok = _check_signature(payment)
    links_ok, links_detail = _check_chain(entries)
    failures = []
    if not evidence_ok:
        failures.append("evidence hash differs")
    if not signature_ok:
        failures.append("permit signature does not verify")
    if not links_ok:
        failures.append("audit links do not verify")
    passed = TOTAL_CHECKS - len(failures)
    verdict = "VERIFIED" if not failures else "FAIL"
    simulated = bool(payment.get("simulated"))
    transaction = payment.get("transaction_hash")
    notices = [
        NOT_CHECKED[0],
        NOT_CHECKED[1].format(transaction=short_hash(transaction, full_hashes=full_hashes) if transaction else "the transaction"),
        NOT_CHECKED[2],
        NOT_CHECKED[3],
    ]
    if simulated:
        notices[1] = NOT_CHECKED_SIMULATED
    signer = payment.get("signer") or ""
    if verdict == "VERIFIED":
        headline = f"{passed} of {TOTAL_CHECKS} offline checks passed, {len(notices)} facts not checked"
    else:
        headline = f"{failures[0]}, {passed} of {TOTAL_CHECKS} offline checks passed"
    number = document["invoice"].get("number") or document["invoice"].get("id")
    lines = [
        f"{verdict}  {headline}",
        _line("Dossier", f"{number}, schema {document['schema']}, {len(entries)} audit entries"),
    ]
    signer_short = short_hash(signer, full_hashes=full_hashes)
    if evidence_ok:
        evidence_line = f"recomputed {short_hash(evidence['hash'], full_hashes=full_hashes)}, matches the file"
    elif recomputed is None:
        evidence_line = "could not be recomputed; the included evidence is malformed"
    elif recomputed != evidence["hash"]:
        evidence_line = "does not match; recomputed"
    else:
        evidence_line = (
            f"recomputed {short_hash(recomputed or '', full_hashes=full_hashes)}, "
            f"the permit signs {short_hash(permit_hash or '', full_hashes=full_hashes)}"
        )
    lines.append(_line("Evidence hash", evidence_line))
    if not evidence_ok and recomputed is not None and recomputed != evidence["hash"]:
        lines.append(f"  {recomputed}")
        lines.append(f"{'':<20}file claims")
        lines.append(f"  {evidence['hash']}")
    signature_line = (
        f"valid for the signer named in the file, {signer_short}"
        if signature_ok
        else f"does not verify for {signer_short}"
    )
    lines.append(_line("Permit signature", signature_line))
    links_line = f"{len(entries)} entries hash correctly and link in order" if links_ok else str(links_detail)
    lines.append(_line("Audit links", links_line))
    if verdict == "VERIFIED":
        lines.append(_line("Not checked", notices[0]))
        lines.extend(f"{'':<20}{notice}" for notice in notices[1:])
    else:
        lines.extend(_wrapped_not_checked(notices))
    lines.append(_line("Source", "this file only; no network, key or database was used"))
    return VerifyOutcome(verdict, 1 if failures else 0, lines)


def _check_evidence(evidence: dict, permit: dict) -> tuple[bool, str | None, str | None]:
    try:
        invoice, accounting, treasury, screening = _evidence_objects(evidence)
        recomputed = compute_evidence_hash(invoice, accounting, treasury, screening)
    except (KeyError, ValueError, TypeError):
        return False, None, permit.get("evidence_hash")
    # The permit signature covers this hash, so a rewritten evidence block that also rewrote
    # its own hash still fails here.
    if recomputed == evidence["hash"] and permit.get("evidence_hash") == evidence["hash"]:
        return True, recomputed, permit.get("evidence_hash")
    return False, recomputed, permit.get("evidence_hash")


def _check_signature(payment: dict) -> bool:
    recovered = _recovered_signer(payment["permit"], payment["signature"])
    claimed = payment.get("signer")
    return bool(recovered and claimed and recovered.lower() == str(claimed).lower())


def _check_chain(entries: list[dict]) -> tuple[bool, str | None]:
    expected_prev = GENESIS_HASH
    previous_id: int | None = None
    for position, entry in enumerate(entries, 1):
        entry_id = entry.get("id")
        if not isinstance(entry_id, int):
            return False, f"entry {position} has no id"
        if previous_id is not None and entry_id <= previous_id:
            return False, f"entry ids are not strictly increasing at entry {position}"
        previous_id = entry_id
        try:
            row = {
                "invoice_id": entry["invoice_id"],
                "event_type": entry["type"],
                "state": entry["state"],
                "payload_json": canonical_json(entry["payload"]),
                "created_at": entry["created_at"],
                "prev_hash": entry["prev_hash"],
                "event_hash": entry["event_hash"],
            }
        except (KeyError, TypeError):
            return False, f"entry {position} is missing a field"
        problem = SQLiteEvidenceStore._chain_problem(row, expected_prev)
        if problem == "prev_hash_does_not_match_the_previous_entry":
            previous = f"entry {position - 1}" if position > 1 else "the previous entry"
            return False, f"entry {position} of {len(entries)} does not link to {previous}"
        if problem:
            return False, f"entry {position} of {len(entries)} does not hash to its recorded value"
        expected_prev = entry["event_hash"]
    return True, None


def _wrapped_not_checked(notices: list[str]) -> list[str]:
    short = list(NOT_CHECKED_SHORT)
    if notices[1] == NOT_CHECKED_SIMULATED:
        short[1] = "the real payment, which is simulated"
    sentence = ", ".join(short[:-1]) + ", and " + short[-1]
    wrapped: list[str] = []
    current = ""
    for word in sentence.split(" "):
        if current and len(current) + 1 + len(word) > 60:
            wrapped.append(current)
            current = word
        else:
            current = word if not current else f"{current} {word}"
    if current:
        wrapped.append(current)
    return [_line("Not checked", wrapped[0])] + [f"{'':<20}{line}" for line in wrapped[1:]]


def _validate(document: Any, reference: str) -> None:
    if not isinstance(document, dict):
        raise DossierRefusal("file is not a dossier object", "the top level is not a JSON object", reference=reference)
    if "schema" not in document:
        raise DossierRefusal("field schema is missing", "a required field is absent, nothing was checked", reference=reference)
    if not isinstance(document["schema"], int) or document["schema"] != SCHEMA_VERSION:
        raise DossierRefusal(
            f"schema version {document['schema']} is not supported",
            f"this verifier reads schema {SCHEMA_VERSION} only",
            reference=reference,
        )
    for name in ("invoice", "payment", "evidence", "audit"):
        if name not in document:
            raise DossierRefusal(
                f"field {name} is missing", "a required field is absent, nothing was checked", reference=reference
            )
        if not isinstance(document[name], dict):
            raise DossierRefusal(f"field {name} is malformed", "the field is not an object", reference=reference)
    entries = document["audit"].get("entries")
    if not isinstance(entries, list):
        raise DossierRefusal("field audit.entries is malformed", "the field is not a list", reference=reference)
    _require_hash(document["evidence"].get("hash"), "evidence_hash", reference)
    permit = document["payment"].get("permit")
    if not isinstance(permit, dict):
        raise DossierRefusal(
            "field permit is missing", "a required field is absent, nothing was checked", reference=reference
        )
    _require_hash(permit.get("evidence_hash"), "permit_hash", reference)
    _require_hash(document["payment"].get("transaction_hash"), "transaction_hash", reference, optional=True)
    signature = document["payment"].get("signature")
    if not isinstance(signature, str) or not _is_hex(_strip_0x(signature), 130):
        raise DossierRefusal(
            "hash in field signature is malformed",
            "a signature is 0x followed by 130 hex characters",
            reference=reference,
        )
    for index, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            raise DossierRefusal(
                f"field audit.entries[{index}] is malformed", "the field is not an object", reference=reference
            )
        _require_hash(entry.get("prev_hash"), f"audit.entries[{index}].prev_hash", reference)
        _require_hash(entry.get("event_hash"), f"audit.entries[{index}].event_hash", reference)


def _require_hash(value: Any, name: str, reference: str, *, optional: bool = False) -> None:
    if value is None:
        if optional:
            return
        raise DossierRefusal(
            f"field {name} is missing", "a required field is absent, nothing was checked", reference=reference
        )
    if not _is_hash(value):
        raise DossierRefusal(
            f"hash in field {name} is malformed",
            "hashes are 0x followed by 64 hex characters",
            reference=reference,
        )


def _is_hash(value: Any) -> bool:
    return isinstance(value, str) and value.startswith("0x") and _is_hex(value[2:], 64)


def _strip_0x(value: str) -> str:
    return value[2:] if value.startswith("0x") else ""


def _is_hex(value: str, length: int) -> bool:
    return len(value) == length and all(character in "0123456789abcdefABCDEF" for character in value)


# --------------------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------------------


def render_export(document: dict[str, Any], filename: str, size_kb: int, *, full_hashes: bool) -> list[str]:
    invoice = document["invoice"]
    payment = document["payment"]
    entries = document["audit"]["entries"]
    kind = "simulated (no on-chain transaction)" if payment["simulated"] else str(payment["network"])
    transaction = payment.get("transaction_hash")
    signer = payment.get("signer") or ""
    return [
        f"EXPORTED  dossier for {invoice['number']}, {len(entries)} audit entries, not yet verified",
        _line("Payment", f"settled, {invoice['amount_usdc']} USDC, {kind}"),
        _line("Transaction", short_hash(transaction, full_hashes=full_hashes) if transaction else "none recorded"),
        _line("Evidence hash", short_hash(document["evidence"]["hash"], full_hashes=full_hashes)),
        _line("Signer in file", f"{short_hash(signer, full_hashes=full_hashes)} (claimed by the file, not proven)"),
        _line("File", f"{filename}, {size_kb} KB"),
        _line("Contains", "invoice evidence, supplier and ERP ids; review before sharing"),
        _line("Next", f"arc-payables-dossier verify {filename}"),
    ]


def render_refusal(exc: DossierRefusal, verb: str) -> list[str]:
    label = "Invoice" if verb == "export" else "Dossier"
    lines = [f"REFUSED  {exc.headline}, nothing {'written' if verb == 'export' else 'was checked'}"]
    if exc.reference:
        lines.append(_line(label, exc.reference))
    lines.append(_line("Reason", exc.reason))
    return lines


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def export(
    store: SQLiteEvidenceStore,
    settings: Any,
    reference: str,
    *,
    out: Path | None = None,
    full_hashes: bool = False,
) -> tuple[list[str], int]:
    invoice = _find_invoice(store, reference)
    if invoice is None:
        refusal = DossierRefusal("invoice was not found", f"no invoice matches {reference}", reference=reference)
        return render_refusal(refusal, "export"), 2
    try:
        document = build_document(store, settings, invoice)
        filename = out or Path(f"dossier-{invoice.invoice_number}.json")
        payload = json.dumps(document, indent=2, sort_keys=True, default=_json_default) + "\n"
        filename.write_text(payload)
    except DossierRefusal as exc:
        return render_refusal(exc, "export"), 2
    except OSError as exc:
        refusal = DossierRefusal(
            "the dossier could not be written",
            f"{type(exc).__name__}: {exc.strerror or exc}",
            reference=invoice.invoice_number,
        )
        return render_refusal(refusal, "export"), 2
    return render_export(document, filename.name, _size_kb(len(payload.encode())), full_hashes=full_hashes), 0


def verify(path: Path, *, full_hashes: bool = False) -> VerifyOutcome:
    reference = path.name
    try:
        document = json.loads(path.read_text())
    except (OSError, ValueError):
        refusal = DossierRefusal("file is not valid JSON", "the file could not be read as JSON", reference=reference)
        return VerifyOutcome("REFUSED", 2, render_refusal(refusal, "verify"))
    try:
        return verify_document(document, reference, full_hashes=full_hashes)
    except DossierRefusal as exc:
        return VerifyOutcome("REFUSED", 2, render_refusal(exc, "verify"))


def _find_invoice(store: SQLiteEvidenceStore, reference: str) -> InvoiceRecord | None:
    invoice = store.get_invoice(reference)
    if invoice is not None:
        return invoice
    for candidate, _state in store.list_invoices():
        if candidate.invoice_number == reference:
            return candidate
    return None


def _size_kb(size: int) -> int:
    return max(1, round(size / 1024))


def _usdc(units: int) -> str:
    return f"{Decimal(units) / USDC_SCALE:.2f}"


def main(argv: list[str] | None = None) -> int:
    from .settings import get_settings

    parser = argparse.ArgumentParser(
        prog="arc-payables-dossier",
        description="Export or verify one settled payment as an offline-checkable JSON dossier.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    export_parser = commands.add_parser("export", help="Write a dossier for one settled payment")
    export_parser.add_argument("invoice", help="Invoice id or invoice number")
    export_parser.add_argument("--out", type=Path, help="Write the dossier here instead of dossier-<number>.json")
    export_parser.add_argument("--full-hashes", action="store_true", help="Print hashes in full")
    verify_parser = commands.add_parser("verify", help="Verify a dossier file offline")
    verify_parser.add_argument("file", type=Path, help="Dossier JSON file")
    verify_parser.add_argument("--full-hashes", action="store_true", help="Print hashes in full")
    args = parser.parse_args(argv)

    if args.command == "export":
        settings = get_settings()
        store = SQLiteEvidenceStore(settings.database_path)
        lines, code = export(store, settings, args.invoice, out=args.out, full_hashes=args.full_hashes)
    else:
        outcome = verify(args.file, full_hashes=args.full_hashes)
        lines, code = outcome.lines, outcome.exit_code
    for line in lines:
        print(line)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
