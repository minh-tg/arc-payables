"""The operator console shell.

The console is deliberately inert without credentials, so these tests check that the shell is
served without a key, that its assets are reachable, and that serving it does not open the API.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest

from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore


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
    assert "#/queue" in body and "#/treasury" in body
    assert "Arc Testnet only" in body


def test_the_console_assets_are_reachable(tmp_path):
    client = _client(tmp_path)
    for asset in ("app.js", "queue.js", "invoice.js", "treasury.js"):
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


WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"
NODE = shutil.which("node")


def _parses_as_module(tmp_path: Path, source: str) -> None:
    """Ask Node to parse the source as an ES module; nothing is executed."""
    target = tmp_path / "module.mjs"
    target.write_text(source)
    result = subprocess.run([NODE, "--check", str(target)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(NODE is None, reason="node is required to parse the console modules")
def test_every_console_module_parses(tmp_path):
    """A syntax error here would leave a blank page that no server-side test would notice."""
    modules = sorted(WEB_DIR.glob("*.js"))
    assert modules, "expected the console modules to be present"
    for module in modules:
        _parses_as_module(tmp_path, module.read_text())


@pytest.mark.skipif(NODE is None, reason="node is required to parse the page script")
def test_the_inline_page_script_parses(tmp_path):
    html = (WEB_DIR / "index.html").read_text()
    scripts = re.findall(r'<script type="module">(.*?)</script>', html, re.DOTALL)
    assert scripts, "the console shell should bootstrap its modules"
    for script in scripts:
        _parses_as_module(tmp_path, script)


def test_every_helper_a_view_imports_exists():
    """The modules import from app.js by name; a rename would otherwise fail only in a browser."""
    exported = set(re.findall(r"export (?:async )?function (\w+)", (WEB_DIR / "app.js").read_text()))
    missing = []
    for module in sorted(WEB_DIR.glob("*.js")):
        if module.name == "app.js":
            continue
        for names in re.findall(r"import \{([^}]+)\} from '\./app\.js'", module.read_text()):
            for name in (item.strip() for item in names.split(",")):
                if name and name not in exported:
                    missing.append(f"{module.name} imports {name}")
    assert not missing, missing
