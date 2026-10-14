"""Release/publication and disposable-container configuration regressions."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("package_check", ROOT / "script/check-package.py")
package_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(package_check)


@pytest.mark.parametrize("names", [
    ["app.js", "app.js"], ["local-docs/private.md"], [".secret"],
    ["data/live.sqlite3"], ["../private"], ["/private"],
])
def test_release_archive_rejects_duplicate_private_and_unsafe_paths(names):
    with pytest.raises(ValueError):
        package_check.validate_members(names)


def test_release_archive_requires_runtime_assets():
    with pytest.raises(ValueError, match="Missing runtime assets"):
        package_check.validate_members(["arc_payables/api.py"], ["arc_payables/web/app.js"])


def test_release_archive_accepts_public_runtime_assets():
    package_check.validate_members(["arc_payables/api.py", "arc_payables/web/app.js"],
                                   ["arc_payables/web/app.js"])


def test_disposable_compose_cannot_load_live_credentials_or_host_data():
    config = yaml.safe_load((ROOT / "deploy/arc-payables/docker-compose.demo.yml").read_text())
    assert config["name"] != "arc-payables"
    assert set(config["services"]) == {"api"}, "A demo must not launch a worker"
    api = config["services"]["api"]
    assert "env_file" not in api and "volumes" not in api
    assert api["read_only"] and api["cap_drop"] == ["ALL"]
    assert api["security_opt"] == ["no-new-privileges:true"]
    assert api["ports"] == ["127.0.0.1:8099:8000"]
    env = api["environment"]
    assert env["PAYMENT_PROVIDER"] == env["ACCOUNTING_PROVIDER"] == "mock"
    assert env["SCREENING_PROVIDER"] == "fixture" and env["AUTH_MODE"] == "demo"
    assert env["SIGNER_BACKEND"] == "env" and env["WORKER_AUTOPAY"] == "false"
    assert env["API_KEY"] == env["APPROVAL_TOKEN"] == ""
    assert env["DATABASE_PATH"].startswith("/app/data/")
    assert not {"CIRCLE_API_KEY", "FRAPPE_API_KEY", "FRAPPE_API_SECRET",
                "LOCAL_PAYMENT_PRIVATE_KEY", "PKCS11_PIN", "OIDC_CLIENT_SECRET"} & env.keys()
    assert any(mount.startswith("/app/data:") and "mode=1777" in mount for mount in api["tmpfs"])
