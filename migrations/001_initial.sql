PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fixtures (
    kind TEXT NOT NULL,
    fixture_key TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (kind, fixture_key)
);

CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    supplier_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    data_json TEXT NOT NULL,
    state TEXT NOT NULL,
    decision_json TEXT,
    evidence_snapshot_json TEXT,
    approval_json TEXT,
    updated_at TEXT NOT NULL,
    UNIQUE (supplier_id, invoice_number)
);

CREATE TABLE IF NOT EXISTS request_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL UNIQUE REFERENCES invoices(id),
    idempotency_key TEXT NOT NULL UNIQUE,
    record_json TEXT NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    event_type TEXT NOT NULL,
    state TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_invoice_id_id ON audit_events(invoice_id, id);
CREATE INDEX IF NOT EXISTS idx_invoices_state ON invoices(state);
CREATE INDEX IF NOT EXISTS idx_payments_state ON payments(state);
