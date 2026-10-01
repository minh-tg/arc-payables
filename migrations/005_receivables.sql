-- Expected cash inflows (receivables).
--
-- The forecast only saw money leaving, so it could not answer whether the balance will
-- cover what is due. One row per expected receipt: a sales invoice the accounting system
-- holds, a customer reference, an amount converted once at the configured rate, and the
-- date the money is expected. Inflows never authorize anything; the forecast walk adds
-- them back to the running balance on their expected date.
CREATE TABLE IF NOT EXISTS receivables (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT NOT NULL UNIQUE,
    customer TEXT NOT NULL,
    reference TEXT NOT NULL,
    amount_units INTEGER NOT NULL CHECK (amount_units > 0),
    currency TEXT NOT NULL,
    expected_date TEXT NOT NULL,
    source TEXT NOT NULL,
    collected_at TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_receivables_expected_date ON receivables(expected_date);
CREATE INDEX IF NOT EXISTS idx_receivables_collected_at ON receivables(collected_at);
