"""Safety boundaries for the credential-free browser smoke harnesses."""
from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "script/smoke-common.sh"


def run_helper(tmp_path, body):
    project = tmp_path / "project"
    scripts = project / "script"
    scripts.mkdir(parents=True)
    helper = scripts / "smoke-common.sh"
    helper.write_bytes(HELPER.read_bytes())
    (project / ".env").write_text("PAYMENT_PROVIDER=circle\nCIRCLE_API_KEY=dotenv-secret\n")
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "print(json.dumps({'argv':sys.argv[1:], 'env':dict(os.environ), "
        "'cwd':os.getcwd(), 'pid':os.getpid(), 'dotenv':Path('.env').exists()}))\n"
        "if Path(sys.argv[0]).name == 'uv' and sys.argv[5:6] == ['env']:\n"
        "    sys.stdout.flush()\n"
        "    os.execvpe('env', sys.argv[5:], os.environ)\n"
    )
    for name in ("uv", "agent-browser", "chromium", "arc-payables-seed"):
        executable = binaries / name
        executable.write_text(stub)
        executable.chmod(0o755)
    env = {
        **os.environ, "PATH": f"{binaries}:{os.environ['PATH']}",
        "PAYMENT_PROVIDER": "circle", "ACCOUNTING_PROVIDER": "erpnext",
        "CIRCLE_API_KEY": "inherited-secret", "ERP_API_KEY": "inherited-erp-secret",
        "API_KEY": "inherited-api-secret", "APPROVAL_TOKEN": "inherited-approval-secret",
        "OIDC_CLIENT_SECRET": "inherited-oidc-secret", "SIGNER_BACKEND": "pkcs11",
        "WORKER_AUTOPAY": "true", "AGENT_BROWSER_AUTO_CONNECT": "true",
        "AGENT_BROWSER_PROFILE": "/private/browser", "AGENT_BROWSER_PROVIDER": "browserbase",
    }
    result = subprocess.run(
        ["bash", "-ceu", f"source {shlex.quote(str(helper))}; {body}"],
        cwd=project, env=env, capture_output=True, text=True, timeout=15,
    )
    records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    return result, records, project


@pytest.mark.parametrize("command", ["arc-payables-seed", "arc-payables-worker --once --autopay --intake"])
def test_mock_command_ignores_credentials_dotenv_and_live_backends(tmp_path, command):
    result, records, project = run_helper(tmp_path, f"mock_command {command}")
    assert result.returncode == 0, result.stderr
    record = records[0]
    env = record["env"]
    assert env["PAYMENT_PROVIDER"] == "mock"
    assert env["ACCOUNTING_PROVIDER"] == "mock"
    assert env["SCREENING_PROVIDER"] == "fixture"
    assert env["SIGNER_BACKEND"] == "env"
    assert env["WORKER_AUTOPAY"] == "false"
    assert env["API_KEY"] == env["APPROVAL_TOKEN"] == ""
    assert not {"CIRCLE_API_KEY", "ERP_API_KEY", "OIDC_CLIENT_SECRET"} & env.keys()
    assert record["argv"][:4] == ["run", "--project", str(project), "--frozen"]
    assert record["cwd"] != str(project) and not record["dotenv"]
    assert Path(env["DATABASE_PATH"]).parent == Path(record["cwd"])
    assert not Path(record["cwd"]).exists(), "Disposable data survived cleanup"


def test_browser_cannot_attach_to_user_profile_or_cloud_provider(tmp_path):
    result, records, _ = run_helper(tmp_path, "browser open http://127.0.0.1:8096/console/")
    assert result.returncode == 0, result.stderr
    record = records[0]
    env = record["env"]
    assert not {"AGENT_BROWSER_AUTO_CONNECT", "AGENT_BROWSER_PROFILE", "AGENT_BROWSER_PROVIDER",
                "CIRCLE_API_KEY", "OIDC_CLIENT_SECRET"} & env.keys()
    assert env["AGENT_BROWSER_DEFAULT_TIMEOUT"] == "10000"
    assert "--session" in record["argv"] and "--namespace" in record["argv"]
    assert "--executable-path" in record["argv"]
    assert not Path(env["HOME"]).exists(), "Browser profile survived cleanup"


def test_background_server_pid_is_the_spawned_command(tmp_path):
    result, records, _ = run_helper(
        tmp_path, 'mock_server uvicorn arc_payables.api:app & SERVER_PID=$!; wait "$SERVER_PID"; echo "server=$SERVER_PID"'
    )
    assert result.returncode == 0, result.stderr
    assert f"server={records[0]['pid']}" in result.stdout


def test_failure_still_removes_disposable_data(tmp_path):
    result, records, _ = run_helper(tmp_path, "mock_command arc-payables-seed; false")
    assert result.returncode != 0
    assert not Path(records[0]["cwd"]).exists()


def test_occupied_port_is_refused_before_starting_an_application(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        result, records, _ = run_helper(tmp_path, f"smoke_require_free_port {port}; mock_command arc-payables-seed")
    assert result.returncode != 0 and "occupied" in result.stderr
    assert not records


@pytest.mark.parametrize("port", ["80", "70000", "abc"])
def test_invalid_port_is_refused(tmp_path, port):
    result, records, _ = run_helper(tmp_path, f"smoke_require_free_port {port}; mock_command arc-payables-seed")
    assert result.returncode != 0
    assert not records


def test_all_browser_scripts_use_the_isolation_helper():
    for name in ("console-smoke.sh", "console-journey-smoke.sh", "oidc-console-smoke.sh"):
        source = (ROOT / "script" / name).read_text()
        assert 'source "$(dirname "$0")/smoke-common.sh"' in source
        assert 'smoke_require_free_port "$PORT"' in source
        assert "--dump-dom" not in source.split("set -euo")[1]
    oidc = (ROOT / "script/oidc-console-smoke.sh").read_text()
    assert oidc.count("env -i PATH=") == 2
    assert oidc.count('uv run --project "$ROOT" --frozen') == 2
    assert '--app-dir "$ROOT/tests"' in oidc


@pytest.mark.parametrize("args", [
    [], ["--live"], ["--screening-unavailable", "--high-reserve"], ["--high-reserve", "extra"],
])
def test_failure_profile_cli_rejects_unspecified_live_or_multiple_profiles(args):
    result = subprocess.run(
        ["bash", str(ROOT / "script/manual-failure-profiles-smoke.sh"), *args],
        capture_output=True, text=True, timeout=5,
    )
    assert result.returncode == 2


@pytest.mark.parametrize("screening,reserve", [("unavailable", "2000"), ("fixture", "1000000000")])
def test_failure_profile_env_overrides_remain_mock_and_credential_free(tmp_path, screening, reserve):
    result, records, _ = run_helper(
        tmp_path,
        f"mock_command env SCREENING_PROVIDER={screening} MIN_RESERVE_USDC={reserve} arc-payables-seed",
    )
    assert result.returncode == 0, result.stderr
    assert len(records) == 2, "The spy must execute env and observe the downstream seed environment"
    final = records[-1]
    env = final["env"]
    assert env["SCREENING_PROVIDER"] == screening and env["MIN_RESERVE_USDC"] == reserve
    assert env["PAYMENT_PROVIDER"] == env["ACCOUNTING_PROVIDER"] == "mock"
    assert env["SIGNER_BACKEND"] == "env" and env["WORKER_AUTOPAY"] == "false"
    assert not {"CIRCLE_API_KEY", "ERP_API_KEY", "OIDC_CLIENT_SECRET"} & env.keys()
    assert env["API_KEY"] == env["APPROVAL_TOKEN"] == ""
    assert not final["dotenv"] and not Path(final["cwd"]).exists()


def test_failure_profile_browser_only_evaluates_never_approves_or_pays():
    source = (ROOT / "script/manual-failure-profiles-smoke.sh").read_text()
    clicks = [line for line in source.splitlines() if line.startswith("browser click ")]
    assert clicks == ["browser click 'button[data-action=evaluate]'"]
    assert "arc-payables-worker" not in source
    assert "assert_unapproved_unpaid" in source
    assert "--host 127.0.0.1" in source
    assert 'smoke_require_free_port "$PORT"' in source
    assert 'source "$(dirname "$0")/smoke-common.sh"' in source
