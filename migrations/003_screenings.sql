-- Screening history per counterparty.
--
-- One row per check, so a risk profile change is visible as a transition rather than a
-- single mutable status. The latest row governs the counterparty's automatic limit.
CREATE TABLE IF NOT EXISTS screenings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_id TEXT NOT NULL,
    wallet TEXT,
    status TEXT NOT NULL,
    tier TEXT NOT NULL,
    provider TEXT,
    dataset TEXT,
    response_hash TEXT,
    matches_json TEXT,
    reason TEXT,
    source TEXT NOT NULL,
    checked_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_screenings_supplier_id ON screenings(supplier_id, id);
