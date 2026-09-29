from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .domain import InvoiceLine, InvoiceRecord, WorkflowState, utcnow


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(f"Cannot encode {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=_json_default)


def invoice_to_dict(invoice: InvoiceRecord) -> dict[str, Any]:
    return asdict(invoice)


# States a decision may assign. Once an invoice has a payment row, a later decision must
# never move the invoice's workflow state back into one of these.
DECISION_STATES = {
    WorkflowState.RECEIVED.value,
    WorkflowState.EVIDENCE_CHECKING.value,
    WorkflowState.ELIGIBLE.value,
    WorkflowState.WAITING.value,
    WorkflowState.HELD.value,
    WorkflowState.ESCALATED.value,
}


def invoice_from_dict(raw: dict[str, Any]) -> InvoiceRecord:
    data = dict(raw)
    data["invoice_date"] = date.fromisoformat(data["invoice_date"])
    data["due_date"] = date.fromisoformat(data["due_date"])
    if data.get("discount_deadline"):
        data["discount_deadline"] = date.fromisoformat(data["discount_deadline"])
    if data.get("created_at"):
        data["created_at"] = datetime.fromisoformat(data["created_at"])
    else:
        data["created_at"] = utcnow()
    data["lines"] = tuple(
        InvoiceLine(
            item_code=line["item_code"],
            quantity=str(line["quantity"]),
            amount_units=int(line["amount_units"]),
            purchase_order_line_id=line.get("purchase_order_line_id"),
            receipt_line_ids=tuple(line.get("receipt_line_ids", ())),
        )
        for line in data.get("lines", ())
    )
    for key in ("purchase_order_ids", "receipt_ids"):
        data[key] = tuple(data.get(key, ()))
    return InvoiceRecord(**data)


class DuplicateInvoiceNumber(ValueError):
    """Raised when a supplier invoice number is already used by another invoice."""


class SQLiteEvidenceStore:
    """Small durable evidence store with explicit SQLite write transactions."""

    def __init__(self, database_path: Path | str):
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=15000")
        return connection

    def initialize(self) -> None:
        migrations = Path(__file__).resolve().parent / "migrations"
        if not migrations.exists():
            migrations = Path(__file__).resolve().parents[2] / "migrations"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
                )
                for path in sorted(migrations.glob("*.sql")):
                    version = path.name
                    seen = connection.execute(
                        "SELECT 1 FROM schema_migrations WHERE version=?", (version,)
                    ).fetchone()
                    if seen:
                        continue
                    buffer = ""
                    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
                        buffer += line
                        if sqlite3.complete_statement(buffer):
                            statement = buffer.strip()
                            if statement:
                                connection.execute(statement)
                            buffer = ""
                    if buffer.strip():
                        raise RuntimeError(f"Incomplete SQL migration: {version}")
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, utcnow().isoformat()),
                    )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def health(self) -> bool:
        with self._connect() as connection:
            connection.execute("SELECT 1 FROM schema_migrations LIMIT 1").fetchone()
        return True

    def create_invoice(self, invoice: InvoiceRecord, idempotency_key: str) -> tuple[InvoiceRecord, bool]:
        raw = invoice_to_dict(invoice)
        request_fingerprint = {key: value for key, value in raw.items() if key not in {"id", "created_at"}}
        request_hash = hashlib.sha256(canonical_json(request_fingerprint).encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                previous = connection.execute(
                    "SELECT request_hash, resource_id FROM request_idempotency WHERE scope='create_invoice' AND idempotency_key=?",
                    (idempotency_key,),
                ).fetchone()
                if previous:
                    if previous["request_hash"] != request_hash:
                        raise ValueError("Idempotency-Key was already used with a different request")
                    record = connection.execute("SELECT data_json FROM invoices WHERE id=?", (previous["resource_id"],)).fetchone()
                    connection.commit()
                    return invoice_from_dict(json.loads(record["data_json"])), False

                existing = connection.execute(
                    "SELECT id, data_json FROM invoices WHERE supplier_id=? AND invoice_number=?",
                    (invoice.supplier_id, invoice.invoice_number),
                ).fetchone()
                if existing:
                    existing_data = json.loads(existing["data_json"])
                    existing_fingerprint = {key: value for key, value in existing_data.items() if key not in {"id", "created_at"}}
                    duplicate_same = hashlib.sha256(canonical_json(existing_fingerprint).encode()).hexdigest() == request_hash
                    if not duplicate_same:
                        raise ValueError("Supplier invoice number already exists with conflicting invoice data")
                    connection.execute(
                        "INSERT INTO request_idempotency(scope,idempotency_key,request_hash,resource_id,created_at) VALUES ('create_invoice',?,?,?,?)",
                        (idempotency_key, request_hash, existing["id"], utcnow().isoformat()),
                    )
                    connection.commit()
                    return invoice_from_dict(existing_data), False

                now = utcnow().isoformat()
                connection.execute(
                    "INSERT INTO invoices(id,supplier_id,invoice_number,data_json,state,updated_at) VALUES(?,?,?,?,?,?)",
                    (invoice.id, invoice.supplier_id, invoice.invoice_number, canonical_json(raw), WorkflowState.RECEIVED.value, now),
                )
                connection.execute(
                    "INSERT INTO request_idempotency(scope,idempotency_key,request_hash,resource_id,created_at) VALUES ('create_invoice',?,?,?,?)",
                    (idempotency_key, request_hash, invoice.id, now),
                )
                self._append_event(connection, invoice.id, "INVOICE_RECEIVED", WorkflowState.RECEIVED.value, {"source_document_hash": invoice.source_document_hash})
                connection.commit()
                return invoice, True
            except Exception:
                connection.rollback()
                raise

    def get_invoice(self, invoice_id: str) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return invoice_from_dict(json.loads(row["data_json"])) if row else None

    def list_invoices(self) -> list[tuple[InvoiceRecord, str]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT data_json,state FROM invoices ORDER BY updated_at DESC").fetchall()
        return [(invoice_from_dict(json.loads(row["data_json"])), row["state"]) for row in rows]

    def list_supplier_invoices(self, supplier_id: str, invoice_number: str) -> list[InvoiceRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT data_json FROM invoices WHERE supplier_id=? AND invoice_number=?",
                (supplier_id, invoice_number),
            ).fetchall()
        return [invoice_from_dict(json.loads(row["data_json"])) for row in rows]

    def update_invoice_record(
        self,
        invoice: InvoiceRecord,
        event_type: str,
        payload: dict | None = None,
    ) -> None:
        """Replace an invoice's stored data, keeping its state and appending an audit event.

        Used when linking a captured invoice to an ERPNext payable, where the payable fields
        become those of the accounting record. The decision is left stale on purpose so the
        caller must re-evaluate against the adopted evidence.
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "UPDATE invoices SET supplier_id=?, invoice_number=?, data_json=?, updated_at=? WHERE id=?",
                    (
                        invoice.supplier_id,
                        invoice.invoice_number,
                        canonical_json(invoice_to_dict(invoice)),
                        utcnow().isoformat(),
                        invoice.id,
                    ),
                )
                self._append_event(
                    connection,
                    invoice.id,
                    event_type,
                    self._state(connection, invoice.id),
                    payload or {},
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise DuplicateInvoiceNumber(
                    "another invoice already uses this supplier invoice number"
                ) from exc
            except Exception:
                connection.rollback()
                raise

    def find_invoice_by_number(self, supplier_id: str, invoice_number: str) -> InvoiceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT data_json FROM invoices WHERE supplier_id=? AND invoice_number=?",
                (supplier_id, invoice_number),
            ).fetchone()
        return invoice_from_dict(json.loads(row["data_json"])) if row else None

    def get_state(self, invoice_id: str) -> str | None:
        with self._connect() as connection:
            row = connection.execute("SELECT state FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return row["state"] if row else None

    def get_decision(self, invoice_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT decision_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return json.loads(row["decision_json"]) if row and row["decision_json"] else None

    def get_evidence_snapshot(self, invoice_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT evidence_snapshot_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return json.loads(row["evidence_snapshot_json"]) if row and row["evidence_snapshot_json"] else None

    def save_evaluation(self, invoice_id: str, decision: dict, state: str, evidence_snapshot: dict) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_payment = connection.execute(
                    "SELECT state FROM payments WHERE invoice_id=?", (invoice_id,)
                ).fetchone()
                effective_state = state
                if existing_payment and state in DECISION_STATES:
                    # An authorization or settlement outranks a decision state. Without this,
                    # a concurrently re-evaluated invoice would report ELIGIBLE after its
                    # payment had already been confirmed and recorded in the ERP.
                    effective_state = existing_payment["state"]
                connection.execute(
                    "UPDATE invoices SET decision_json=?, evidence_snapshot_json=?, state=?, updated_at=? WHERE id=?",
                    (canonical_json(decision), canonical_json(evidence_snapshot), effective_state, utcnow().isoformat(), invoice_id),
                )
                self._append_event(connection, invoice_id, "DECISION_RECORDED", effective_state, {"action": decision["action"], "evidence_hash": decision["evidence_hash"], "policy_version": decision["policy_version"], "checks": decision["policy_checks"]})
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def set_state(self, invoice_id: str, state: str, event_type: str, payload: dict | None = None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("UPDATE invoices SET state=?, updated_at=? WHERE id=?", (state, utcnow().isoformat(), invoice_id))
                self._append_event(connection, invoice_id, event_type, state, payload or {})
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def record_approval(self, invoice_id: str, approval: dict) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT approval_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
                prior = json.loads(row["approval_json"]) if row and row["approval_json"] else []
                prior.append(approval)
                connection.execute(
                    "UPDATE invoices SET approval_json=?, updated_at=? WHERE id=?",
                    (canonical_json(prior), utcnow().isoformat(), invoice_id),
                )
                self._append_event(connection, invoice_id, "HUMAN_APPROVAL_RECORDED", self._state(connection, invoice_id), {"reviewer": approval["reviewer"], "approved": approval["approved"], "acknowledged_checks": approval["acknowledged_checks"]})
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def get_approval(self, invoice_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT approval_json FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return json.loads(row["approval_json"]) if row and row["approval_json"] else None

    def create_payment(self, invoice_id: str, payment: dict) -> tuple[dict, bool]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute("SELECT record_json FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
                if existing:
                    connection.commit()
                    return json.loads(existing["record_json"]), False
                connection.execute(
                    "INSERT INTO payments(payment_id,invoice_id,idempotency_key,record_json,state,updated_at) VALUES(?,?,?,?,?,?)",
                    (payment["payment_id"], invoice_id, payment["idempotency_key"], canonical_json(payment), payment["state"], utcnow().isoformat()),
                )
                connection.execute("UPDATE invoices SET state=?, updated_at=? WHERE id=?", (WorkflowState.AUTHORIZED.value, utcnow().isoformat(), invoice_id))
                self._append_event(connection, invoice_id, "PAYMENT_AUTHORIZED", WorkflowState.AUTHORIZED.value, {"payment_id": payment["payment_id"], "permit_id": payment["permit"]["payment_id"], "evidence_hash": payment["permit"]["evidence_hash"]})
                connection.commit()
                return payment, True
            except Exception:
                connection.rollback()
                raise

    def get_payment(self, invoice_id: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT record_json FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        return json.loads(row["record_json"]) if row else None

    def update_payment(self, invoice_id: str, updates: dict, state: str, event_type: str, payload: dict | None = None) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT record_json FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
                if not row:
                    raise ValueError("Payment authorization does not exist")
                record = json.loads(row["record_json"])
                record.update(updates)
                record["state"] = state
                connection.execute(
                    "UPDATE payments SET record_json=?, state=?, updated_at=? WHERE invoice_id=?",
                    (canonical_json(record), state, utcnow().isoformat(), invoice_id),
                )
                connection.execute("UPDATE invoices SET state=?, updated_at=? WHERE id=?", (state, utcnow().isoformat(), invoice_id))
                self._append_event(connection, invoice_id, event_type, state, payload or {})
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def claim_erp_writeback(self, invoice_id: str, lease_seconds: int = 120) -> bool:
        now = utcnow()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute("SELECT record_json,state FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
                if not row:
                    connection.rollback()
                    return False
                record = json.loads(row["record_json"])
                if record.get("erp_entry_id") or record.get("erp_status") == "RECORDED":
                    connection.rollback()
                    return False
                claimed_at = record.get("erp_claimed_at")
                if claimed_at:
                    age = (now - datetime.fromisoformat(claimed_at)).total_seconds()
                    if age < lease_seconds:
                        connection.rollback()
                        return False
                record["erp_status"] = "WRITING"
                record["erp_claimed_at"] = now.isoformat()
                connection.execute(
                    "UPDATE payments SET record_json=?,state=?,updated_at=? WHERE invoice_id=?",
                    (canonical_json(record), WorkflowState.ERP_PENDING.value, now.isoformat(), invoice_id),
                )
                connection.execute(
                    "UPDATE invoices SET state=?,updated_at=? WHERE id=?",
                    (WorkflowState.ERP_PENDING.value, now.isoformat(), invoice_id),
                )
                self._append_event(connection, invoice_id, "ERP_WRITEBACK_CLAIMED", WorkflowState.ERP_PENDING.value, {})
                connection.commit()
                return True
            except Exception:
                connection.rollback()
                raise

    def events(self, invoice_id: str) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute("SELECT event_type,state,payload_json,created_at FROM audit_events WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall()
        return [
            {"type": row["event_type"], "state": row["state"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]}
            for row in rows
        ]

    def seed_fixture(self, kind: str, key: str, value: dict) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO fixtures(kind,fixture_key,data_json) VALUES(?,?,?) ON CONFLICT(kind,fixture_key) DO UPDATE SET data_json=excluded.data_json",
                (kind, key, canonical_json(value)),
            )

    def get_supplier_fixture(self, supplier_id: str) -> dict | None:
        return self._fixture("supplier", supplier_id)

    def get_order_fixtures(self, order_ids: tuple[str, ...]) -> list[dict]:
        return [value for item in order_ids if (value := self._fixture("purchase_order", item)) is not None]

    def get_receipt_fixtures(self, receipt_ids: tuple[str, ...]) -> list[dict]:
        return [value for item in receipt_ids if (value := self._fixture("receipt", item)) is not None]

    def _fixture(self, kind: str, key: str) -> dict | None:
        return self.get_fixture(kind, key)

    def get_fixture(self, kind: str, key: str) -> dict | None:
        with self._connect() as connection:
            row = connection.execute("SELECT data_json FROM fixtures WHERE kind=? AND fixture_key=?", (kind, key)).fetchone()
        return json.loads(row["data_json"]) if row else None

    @staticmethod
    def _state(connection: sqlite3.Connection, invoice_id: str) -> str:
        row = connection.execute("SELECT state FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        return row["state"] if row else WorkflowState.RECEIVED.value

    @staticmethod
    def _append_event(connection: sqlite3.Connection, invoice_id: str, event_type: str, state: str, payload: dict) -> None:
        connection.execute(
            "INSERT INTO audit_events(invoice_id,event_type,state,payload_json,created_at) VALUES(?,?,?,?,?)",
            (invoice_id, event_type, state, canonical_json(payload), utcnow().isoformat()),
        )
