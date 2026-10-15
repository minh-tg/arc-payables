"""One mock worker pass in a temporary directory, and the refusal that keeps it mock."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script/unattended-loop-demo.sh"

LIVE_CREDENTIALS = (
    "CIRCLE_API_KEY",
    "CIRCLE_ENTITY_SECRET",
    "CIRCLE_WALLET_ID",
    "CIRCLE_GUARD_ADDRESS",
    "FRAPPE_URL",
    "FRAPPE_API_KEY",
    "FRAPPE_API_SECRET",
    "PLANNER_BASE_URL",
    "PLANNER_MODEL",
    "PLANNER_API_KEY",
    "OPENSANCTIONS_API_KEY",
    "ALERT_WEBHOOK_URL",
    "PERMIT_SIGNING_PRIVATE_KEY",
    "LOCAL_PAYMENT_PRIVATE_KEY",
    "PAYMENT_GUARD_PAUSER_KEY",
)


def _clean_env(**overrides: str) -> dict[str, str]:
    """The ambient environment a normal shell has, minus every live provider setting."""
    env = {key: value for key, value in os.environ.items() if key not in LIVE_CREDENTIALS}
    for key in ("PAYMENT_PROVIDER", "ACCOUNTING_PROVIDER", "SCREENING_PROVIDER"):
        env.pop(key, None)
    env.update(overrides)
    return env


def _run(env: dict[str, str], **kwargs):
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, **kwargs
    )


def test_the_demo_runs_a_real_mock_pass_and_removes_its_temporary_files(tmp_path):
    workdir = tmp_path / "tmp"
    workdir.mkdir()
    result = _run(_clean_env(TMPDIR=str(workdir)))

    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "PASS  1 simulated payment in one worker pass, mock providers"
    assert lines[1] == "Mode:               mock payment, mock accounting, fixture screening"
    assert lines[2] == "Storage:            temporary directory, removed on exit"
    assert lines[3] == "Pass outcome:       ok"
    step = re.compile(r"^  [a-z_]+ +[0-9]+/[0-9]+ acted( \([0-9]+ failed\))?$")
    assert step.match(lines[4]) and step.match(lines[5])
    assert re.match(r"^Simulated payment:  \S+, [0-9]+\.[0-9]{2} USDC$", lines[-2])
    assert lines[-1] == "Funds moved:        none, nothing is sent on chain"
    assert list(workdir.iterdir()) == [], "the temporary directory was not cleaned up"


@pytest.mark.parametrize(
    ("name", "value"),
    [("PAYMENT_PROVIDER", "circle"), ("ACCOUNTING_PROVIDER", "frappe"), ("SCREENING_PROVIDER", "opensanctions")],
)
def test_the_demo_refuses_a_live_provider_setting(tmp_path, name, value):
    result = _run(_clean_env(TMPDIR=str(tmp_path), **{name: value}))

    assert result.returncode == 2
    lines = result.stdout.splitlines()
    assert lines[0] == "FAIL  live provider settings were found and refused"
    assert lines[1] == "Mode:               not started"
    assert lines[2] == f"Reason:             {name}={value} is set; unset it and run again"
    assert "Pass outcome" not in result.stdout
    assert list(tmp_path.iterdir()) == [], "a refused run created files"


@pytest.mark.parametrize("name", LIVE_CREDENTIALS)
def test_the_demo_refuses_a_live_credential_without_printing_its_value(tmp_path, name):
    secret = "do-not-print-this-value"
    result = _run(_clean_env(TMPDIR=str(tmp_path), **{name: secret}))

    assert result.returncode == 2
    assert f"Reason:             {name} is set; unset it and run again" in result.stdout
    assert secret not in result.stdout
    assert list(tmp_path.iterdir()) == [], "a refused run created files"


def test_the_demo_does_not_read_a_project_dotenv(tmp_path):
    """A .env in the invoking directory must not select a live path for the worker."""
    (tmp_path / ".env").write_text("PAYMENT_PROVIDER=circle\nCIRCLE_API_KEY=leaked\n")
    workdir = tmp_path / "tmp"
    workdir.mkdir()

    result = _run(_clean_env(TMPDIR=str(workdir)), cwd=tmp_path)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0].startswith("PASS  ")
    assert "leaked" not in result.stdout


def test_the_worker_receives_only_mock_settings_and_no_inherited_secret(tmp_path):
    binary = tmp_path / "bin"
    binary.mkdir()
    probe_file = tmp_path / "probe.jsonl"
    stub = binary / "uv"
    stub.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "probe = pathlib.Path(__file__).resolve().parent.parent / 'probe.jsonl'\n"
        "with open(probe, 'a') as handle:\n"
        "    handle.write(json.dumps({'args': args, 'env': dict(os.environ), 'cwd': str(pathlib.Path.cwd())}) + '\\n')\n"
        "if 'arc-payables-worker' in args:\n"
        "    print('pass ok: reconcile=0/0, writeback=1/1, intake=2/2, autopay=1/1, rescreen=1/1, observe=1/1 (2 failed)')\n"
        "elif 'python' in args:\n"
        "    print('1|ACME-INV-2026-001|250.00')\n"
    )
    stub.chmod(0o755)
    workdir = tmp_path / "tmp"
    workdir.mkdir()

    result = _run(
        _clean_env(
            PATH=str(binary) + os.pathsep + os.environ["PATH"],
            TMPDIR=str(workdir),
        )
    )

    assert result.returncode == 0, result.stderr
    probes = [json.loads(line) for line in probe_file.read_text().splitlines()]
    assert [probe["args"][-1] for probe in probes] == ["arc-payables-seed", "--intake", "-"]
    for probe in probes:
        env = probe["env"]
        assert env["PAYMENT_PROVIDER"] == env["ACCOUNTING_PROVIDER"] == "mock"
        assert env["SCREENING_PROVIDER"] == "fixture" and env["DECISION_LAYER"] == "heuristics"
        assert env["AUTH_MODE"] == "demo" and env["SIGNER_BACKEND"] == "env"
        for name in LIVE_CREDENTIALS:
            assert not env.get(name), f"{name} reached the worker"
        assert Path(env["DATABASE_PATH"]).parent == Path(probe["cwd"])
        assert Path(env["BACKUP_DIRECTORY"]).parent == Path(probe["cwd"])
        assert Path(probe["cwd"]).is_relative_to(workdir)
    assert Path(probes[0]["cwd"]).exists() is False, "the temporary directory was not cleaned up"
    # The step lines come from the pass report, and a failed step keeps its count.
    assert "  writeback         1/1 acted" in result.stdout
    assert "  observe           1/1 acted (2 failed)" in result.stdout
    assert "Simulated payment:  ACME-INV-2026-001, 250.00 USDC" in result.stdout
