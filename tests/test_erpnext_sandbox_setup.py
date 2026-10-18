"""The sandbox setup script stays on this machine, needs no prompts, and never echoes a secret."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script" / "erpnext-sandbox-setup.sh"
BASH = shutil.which("bash")


def run(args, path, include_system_path=True, **env):
    search = f"{path}:{os.defpath}" if include_system_path else str(path)
    return subprocess.run(
        [BASH, str(SCRIPT), *args], capture_output=True, text=True,
        env={"PATH": search, "HOME": env.pop("HOME", "/nonexistent-home"), **env},
    )


@pytest.fixture
def stub_tools(tmp_path):
    """Stand-ins for every program the real pass starts; each one records that it ran."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    marker = tmp_path / "invoked"
    tools = ("podman", "podman-compose", "docker", "python3", "uv", "bench", "curl")
    for name in tools:
        stub = bindir / name
        stub.write_text(f"#!/usr/bin/env bash\nprintf '%s\\n' {name} >> '{marker}'\nexit 0\n")
        stub.chmod(0o755)
    return bindir, marker


def test_the_script_parses():
    assert subprocess.run([BASH, "-n", str(SCRIPT)], capture_output=True).returncode == 0


def test_dry_run_prints_the_seven_steps_and_starts_nothing(stub_tools):
    bindir, marker = stub_tools
    result = run(["--dry-run"], bindir)
    assert result.returncode == 0, result.stderr
    numbered = dict(re.findall(r"(?m)^(\d)\. (.+)$", result.stdout))
    assert sorted(numbered) == [str(n) for n in range(1, 8)]
    assert "podman-compose" in numbered["1"]
    assert "docker-compose.yml" in numbered["2"]
    assert "new-site" in SCRIPT.read_text()
    assert not marker.exists(), marker.read_text()


def test_dry_run_is_the_default_plan_and_names_the_tool(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = run(["--dry-run"], empty, include_system_path=False)
    assert result.returncode == 0, result.stderr
    assert "1. Container tool:" in result.stdout
    assert "none found" in result.stdout


@pytest.mark.parametrize("args", [[], ["--dry-run"]])
def test_a_remote_erpnext_url_is_refused_before_anything_runs(args, stub_tools):
    bindir, marker = stub_tools
    result = run(args, bindir, FRAPPE_URL="https://erp.example.com")
    assert result.returncode == 2
    assert "must be http://127.0.0.1 or http://localhost" in result.stderr
    assert not marker.exists(), marker.read_text()


def test_help_prints_the_flags_and_starts_nothing(stub_tools):
    bindir, marker = stub_tools
    result = run(["--help"], bindir)
    assert result.returncode == 0
    assert "--dry-run" in result.stdout and "--help" in result.stdout
    assert not marker.exists(), marker.read_text()


def test_an_unknown_argument_is_refused(stub_tools):
    bindir, _ = stub_tools
    result = run(["--apply"], bindir)
    assert result.returncode == 2
    assert "Unknown argument: --apply" in result.stderr


def test_no_echo_or_printf_line_prints_a_password_or_key():
    printed = [
        line for line in SCRIPT.read_text().splitlines()
        if line.lstrip().startswith(("echo", "printf"))
    ]
    assert printed
    for line in printed:
        for name in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", line):
            assert not name.upper().endswith(("PASSWORD", "SECRET", "KEY", "TOKEN")), line


def test_the_generated_secrets_only_reach_their_files():
    text = SCRIPT.read_text()
    # The Administrator password is generated once and passed as a bench argument from the file.
    assert "secrets.token_urlsafe" in text
    assert '--admin-password "$(cat "$ADMIN_PASSWORD_FILE")"' in text
    assert "chmod 600" in text
    # Keys are parsed out of the bench output inside python and written with mode 600.
    assert "os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600" in text
    assert "FRAPPE_API_SECRET=" in text
    assert "set -x" not in text


def test_the_pass_is_idempotent_and_never_forces_or_drops_anything():
    text = SCRIPT.read_text()
    assert text.count("already exists") >= 3
    assert "wizard skipped" in text and "Kept the existing API key pair" in text
    assert "--force" not in text
    assert "drop-site" not in text
    assert "down -v" not in text
    assert "rm -rf" in text and '"$TMPDIR_RUN"' in text
    # The wrapper for the Podman API socket is the proven one-line fallback.
    assert 'podman --remote --url "unix:///run/user/$(id -u)/podman/podman.sock"' in text
    assert "--podman-path" in text
    # The wizard call is the non-interactive one, not the browser flow.
    assert "frappe.desk.page.setup_wizard.setup_wizard.setup_complete" in text
    assert "--set-default" in text and "--install-app erpnext" in text


def test_the_credentials_file_lives_in_a_git_ignored_directory():
    ignored = subprocess.run(
        ["git", "check-ignore", "-q", ".secret/erpnext-sandbox.env"], cwd=ROOT
    ).returncode
    assert ignored == 0


def test_the_dry_run_leaves_the_repository_untouched(stub_tools):
    bindir, _ = stub_tools
    before = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert run(["--dry-run"], bindir).returncode == 0
    after = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True
    ).stdout
    assert before == after
