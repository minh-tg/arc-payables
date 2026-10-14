"""Safety regressions for the manual-only, disposable mock console launcher."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "script/manual-readiness-demo.sh"


@pytest.mark.parametrize(
    ("args", "screening", "reserve"),
    [([], "fixture", "2000"), (["--screening-unavailable"], "unavailable", "2000"),
     (["--high-reserve"], "fixture", "1000000000")],
)
def test_manual_launcher_isolates_credentials_dotenv_and_database(tmp_path, args, screening, reserve):
    binary = tmp_path / "bin"
    binary.mkdir()
    stub = binary / "uv"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "print('PROBE=' + json.dumps({'env': dict(os.environ), 'cwd': str(pathlib.Path.cwd()), "
        "'dotenv': pathlib.Path('.env').exists(), 'args': sys.argv[1:]}))\n"
    )
    stub.chmod(0o755)
    (tmp_path / ".env").write_text("PAYMENT_PROVIDER=circle\n")
    result = subprocess.run(
        ["bash", str(LAUNCHER), *args], cwd=tmp_path, capture_output=True, text=True, check=True,
        env={**os.environ, "PATH": str(binary) + os.pathsep + os.environ["PATH"],
             "PAYMENT_PROVIDER": "circle", "ACCOUNTING_PROVIDER": "frappe", "AUTH_MODE": "oidc",
             "CIRCLE_API_KEY": "must-not-be-forwarded", "PERMIT_SIGNING_PRIVATE_KEY": "must-not-be-forwarded",
             "FRAPPE_API_SECRET": "must-not-be-forwarded", "ALERT_WEBHOOK_URL": "https://invalid.example",
             "PORT": "8099"},
    )
    probes = [json.loads(line.removeprefix("PROBE=")) for line in result.stdout.splitlines()
              if line.startswith("PROBE=")]
    assert len(probes) == (2 if args else 1)
    if args:
        assert probes[0]["args"] == ["run", "--project", str(ROOT), "--frozen", "arc-payables-seed"]
    probe = probes[-1]
    env = probe["env"]
    assert env["PAYMENT_PROVIDER"] == env["ACCOUNTING_PROVIDER"] == "mock"
    assert env["AUTH_MODE"] == "demo" and env["WORKER_AUTOPAY"] == "false"
    assert env["SCREENING_PROVIDER"] == screening
    assert env["MIN_RESERVE_USDC"] == reserve
    assert env["DECISION_LAYER"] == "heuristics"
    for key in ("CIRCLE_API_KEY", "PERMIT_SIGNING_PRIVATE_KEY", "FRAPPE_API_SECRET", "ALERT_WEBHOOK_URL"):
        assert key not in env
    assert not probe["dotenv"]
    assert probe["cwd"] != str(tmp_path)
    assert Path(env["DATABASE_PATH"]).parent == Path(probe["cwd"])
    assert Path(env["BACKUP_DIRECTORY"]).parent == Path(probe["cwd"])
    assert not Path(probe["cwd"]).exists(), "disposable files were not cleaned up"
    assert probe["args"] == ["run", "--project", str(ROOT), "--frozen", "uvicorn",
                             "arc_payables.api:app", "--host", "127.0.0.1", "--port", "8099"]


@pytest.mark.parametrize("args,port", [(["--live"], "8097"), ([], "80000"), ([], "abc"),
                                      (["--high-reserve", "--screening-unavailable"], "8097")])
def test_manual_launcher_rejects_unknown_profiles_and_invalid_ports(tmp_path, args, port):
    result = subprocess.run(["bash", str(LAUNCHER), *args], cwd=tmp_path, text=True,
                            capture_output=True, env={**os.environ, "PORT": port})
    assert result.returncode == 2
