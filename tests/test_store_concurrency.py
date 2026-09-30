"""SQLite must let the API read while the worker commits a write."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from arc_payables.store import SQLiteEvidenceStore


def test_initialize_persists_wal_mode(tmp_path: Path):
    store = SQLiteEvidenceStore(tmp_path / "wal.sqlite3")
    store.initialize()

    with store._connect() as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"


def test_reader_snapshot_does_not_block_worker_commit(tmp_path: Path):
    """Rollback journal mode would reject the writer's commit while this read snapshot is open."""
    store = SQLiteEvidenceStore(tmp_path / "concurrency.sqlite3")
    store.initialize()

    reader = store._connect()
    writer = sqlite3.connect(store.path, timeout=0, isolation_level=None)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT COUNT(*) FROM worker_runs").fetchone()[0] == 0

        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO worker_runs(started_at, finished_at, outcome, detail_json) VALUES (?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "2026-01-01T00:00:01+00:00", "ok", "{}"),
        )
        writer.commit()

        # The open reader keeps its original snapshot while the writer's commit is already durable.
        assert reader.execute("SELECT COUNT(*) FROM worker_runs").fetchone()[0] == 0
        reader.commit()
        assert reader.execute("SELECT COUNT(*) FROM worker_runs").fetchone()[0] == 1
    finally:
        if reader.in_transaction:
            reader.rollback()
        reader.close()
        writer.close()
