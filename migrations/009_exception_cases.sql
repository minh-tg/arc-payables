-- Exception cases: what a held invoice needs corrected, and what happened since.
--
-- One case per invoice, opened by an evaluation that left the invoice escalated or held. The items
-- are the failing checks as that evaluation left them, each naming the accounting record that would
-- have to change and the role that usually handles that record. The events are the case's own
-- history: opening, alert delivery, a change in the source evidence, and the result of each
-- re-evaluation. A case authorizes nothing; it is a description of what a decision already decided.
CREATE TABLE IF NOT EXISTS exception_cases (
    invoice_id TEXT PRIMARY KEY REFERENCES invoices(id),
    status TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    last_checked_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS exception_case_items (
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    position INTEGER NOT NULL,
    check_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    fix TEXT NOT NULL,
    source_type TEXT NOT NULL,
    source_id TEXT,
    owner_role TEXT NOT NULL,
    PRIMARY KEY (invoice_id, position)
);

CREATE TABLE IF NOT EXISTS exception_case_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id TEXT NOT NULL REFERENCES invoices(id),
    event_type TEXT NOT NULL,
    detail TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exception_case_events_invoice ON exception_case_events(invoice_id, id);
