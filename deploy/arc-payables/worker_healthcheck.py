"""Docker healthcheck for a worker process that is alive but may have stopped making passes."""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


def is_healthy(database: Path, *, now: datetime | None = None) -> bool:
    """Require a recent recorded pass; do not infer health from the process still existing."""
    if not database.is_file():
        return False
    try:
        connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
        try:
            row = connection.execute(
                "SELECT finished_at FROM worker_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    if not row:
        return False
    try:
        finished = datetime.fromisoformat(row[0])
    except (TypeError, ValueError):
        return False
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    age_seconds = (now - finished).total_seconds()
    interval = max(1, int(os.environ.get("WORKER_INTERVAL_SECONDS", "60")))
    max_age = max(900, interval * 5)
    return -60 <= age_seconds <= max_age


def main() -> int:
    database = Path(os.environ.get("DATABASE_PATH", "data/arc_payables.sqlite3"))
    if is_healthy(database):
        return 0
    print("worker has no recent recorded pass", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
