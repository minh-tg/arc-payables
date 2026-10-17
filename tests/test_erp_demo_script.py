"""The demo launcher stays disposable, loopback-only and credential-free."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script" / "erp-demo.sh"


def test_the_launcher_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_the_launcher_simulates_payments_and_binds_to_loopback_only():
    text = SCRIPT.read_text()
    assert "PAYMENT_PROVIDER=mock" in text
    assert text.count("--host 127.0.0.1") == 2
    assert "0.0.0.0" not in text
    assert "env -i" in text, "the launcher must not inherit the caller's credentials"


def test_the_launcher_holds_only_throwaway_keys():
    text = SCRIPT.read_text()
    assert "CIRCLE_" not in text and "PRIVATE_KEY" not in text
    assert re.search(r"FRAPPE_API_KEY=sim-key FRAPPE_API_SECRET=sim-secret", text)
    assert "sops" not in text.lower()


def test_the_console_names_the_simulated_payments_with_erpnext_connected_setup():
    app = (ROOT / "src" / "arc_payables" / "web" / "app.js").read_text()
    assert "Simulated payments · ERPNext connected" in app
