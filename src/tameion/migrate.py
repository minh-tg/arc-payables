from __future__ import annotations

from .settings import get_settings
from .store import SQLiteEvidenceStore


def main() -> None:
    settings = get_settings()
    SQLiteEvidenceStore(settings.database_path).initialize()
    print("Database migrations applied.")


if __name__ == "__main__":
    main()
