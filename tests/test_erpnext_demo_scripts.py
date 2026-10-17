"""The ERPNext demo scripts stay on this machine and never echo a credential."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "script" / "erpnext-demo.sh"
RESET = ROOT / "script" / "erpnext-demo-reset.sh"
SECRET = "very-secret-value-do-not-print"


@pytest.mark.parametrize("script", [DEMO, RESET])
def test_the_script_parses(script):
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0


@pytest.mark.parametrize("script", [DEMO, RESET])
def test_a_remote_erpnext_url_is_refused_before_any_request(script, tmp_path):
    env_file = tmp_path / "remote.env"
    env_file.write_text(f"FRAPPE_URL=https://erp.example.com\nFRAPPE_API_KEY=k\nFRAPPE_API_SECRET={SECRET}\n")
    result = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ERP_ENV_FILE": str(env_file)},
    )
    assert result.returncode == 2
    assert "must be http://127.0.0.1 or http://localhost" in result.stderr
    assert SECRET not in result.stdout + result.stderr


def test_a_missing_credentials_file_is_a_clear_error(tmp_path):
    result = subprocess.run(
        ["bash", str(DEMO)], capture_output=True, text=True,
        env={"PATH": os.environ["PATH"], "HOME": str(tmp_path), "ERP_ENV_FILE": str(tmp_path / "absent.env")},
    )
    assert result.returncode == 2 and "No sandbox credentials" in result.stderr


def test_the_demo_simulates_payments_clears_the_environment_and_binds_to_loopback():
    text = DEMO.read_text()
    assert "PAYMENT_PROVIDER=mock" in text
    assert "env -i" in text
    assert "--host 127.0.0.1" in text and "0.0.0.0" not in text
    assert "CIRCLE_" not in text and "PRIVATE_KEY" not in text and "sops" not in text.lower()


def test_neither_script_prints_the_key_or_secret():
    for script in (DEMO, RESET):
        text = script.read_text()
        for name in ("key", "secret"):
            assert f'echo "${name}' not in text and f"printf '%s\\n' \"${name}" not in text
    assert "FRAPPE_API_SECRET" not in "\n".join(
        line for line in DEMO.read_text().splitlines() if line.lstrip().startswith(("echo", "printf"))
    )


def test_the_credentials_file_lives_in_a_git_ignored_directory():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".secret/erpnext-sandbox.env"], cwd=ROOT
    ).returncode
    assert ignored == 0
