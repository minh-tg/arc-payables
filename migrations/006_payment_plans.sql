-- Plans are advisory, never authorization. Workers rebuild instead of resuming stale plans.
CREATE TABLE payment_plans (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    invoice_id TEXT REFERENCES invoices(id),
    digest TEXT NOT NULL,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    outcome_json TEXT NOT NULL DEFAULT '{}',
    finished_at TEXT
);
CREATE INDEX payment_plans_recent ON payment_plans(created_at DESC);
