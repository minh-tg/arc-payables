-- Circle webhook notifications. One row per signed notification, claimed by notification id
-- before any other work, so a replayed delivery is a no-op success. The row is the replay guard
-- and the record of what arrived. It changes no payment and no invoice: a notification is not a
-- settlement, and the worker's own chain-checked reconcile remains the only thing that may move a
-- payment forward.
CREATE TABLE IF NOT EXISTS circle_notifications (
    notification_id TEXT PRIMARY KEY,
    key_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    provider_transaction_id TEXT,
    invoice_id TEXT REFERENCES invoices(id),
    outcome TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_circle_notifications_invoice ON circle_notifications(invoice_id);
