-- Worker passes.
--
-- One row per pass, so the loop's health is visible to a process that is not the loop. The API
-- serves /metrics from its own process while the worker runs as its own, and a restart must not
-- erase the fact that the loop stopped succeeding an hour ago.
CREATE TABLE IF NOT EXISTS worker_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    detail_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_worker_runs_started_at ON worker_runs(started_at DESC);
