"""The operator console shell.

The console is deliberately inert without credentials, so these tests check that the shell is
served without a key, that its assets are reachable, and that serving it does not open the API.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from tameion.api import create_app
from tameion.seed import seed_demo
from tameion.settings import Settings
from tameion.store import SQLiteEvidenceStore


def _client(tmp_path: Path) -> TestClient:
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "console.sqlite3",
        api_key="console-key",
        min_reserve_usdc=Decimal("0"),
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    return TestClient(create_app(settings=settings, store=store))


def test_the_console_shell_is_served_without_an_api_key(tmp_path):
    response = _client(tmp_path).get("/console/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    # The shell names the views it offers and states the testnet boundary it operates within.
    assert "operator console" in body
    assert "#/queue" in body
    assert "Arc Testnet only" in body


def test_the_console_assets_are_reachable(tmp_path):
    client = _client(tmp_path)
    for asset in ("app.js", "queue.js"):
        response = client.get(f"/console/{asset}")
        assert response.status_code == 200, asset
        assert "javascript" in response.headers["content-type"], asset


def test_serving_the_console_does_not_open_the_api(tmp_path):
    """The shell holds no data; the API behind it still requires a key."""
    client = _client(tmp_path)
    assert client.get("/console/").status_code == 200
    assert client.get("/invoices").status_code == 401
    assert client.get("/invoices", headers={"X-API-Key": "console-key"}).status_code == 200


def test_unknown_console_paths_are_not_served(tmp_path):
    client = _client(tmp_path)
    assert client.get("/console/does-not-exist.js").status_code == 404
