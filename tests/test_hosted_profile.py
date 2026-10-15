"""The hosted mock profile: it must refuse a live path, and it must label itself."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.seed import seed_demo
from arc_payables.settings import HOSTED_ALLOWED_SECRETS, Settings, get_settings, hosted_forbidden_fields, looks_like_a_secret_field
from arc_payables.store import SQLiteEvidenceStore

ROOT = Path(__file__).resolve().parents[1]
SECRET = "hosted-secret-that-must-not-be-printed"


def _client(tmp_path, **overrides) -> TestClient:
    # The hosted console authenticates as the demo identity, so it carries no API key. The
    # profile refuses one if it is set.
    settings = Settings(_env_file=None, database_path=tmp_path / "hosted.sqlite3", **overrides)
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    return TestClient(create_app(settings=settings, store=store))


def test_every_credential_field_is_refused_or_allowed_with_a_reason():
    """A new secret field cannot be forgotten: the walk forces it into one of the two sets."""
    refused = set(hosted_forbidden_fields())
    for name, field in Settings.model_fields.items():
        if not looks_like_a_secret_field(name, field):
            continue
        assert name in refused or name in HOSTED_ALLOWED_SECRETS, (
            f"{name} looks like a credential but the hosted profile does not decide about it"
        )
    assert set(HOSTED_ALLOWED_SECRETS) <= set(Settings.model_fields)


def test_the_named_application_and_identity_secrets_are_refused():
    refused = set(hosted_forbidden_fields())
    for name in ("api_key", "approval_token", "metrics_api_key", "oidc_client_secret", "circle_api_key", "circle_entity_secret"):
        assert name in refused, f"{name} is a secret the hosted profile must refuse"


def test_the_hosted_profile_accepts_only_the_mock_configuration(monkeypatch):
    for name in hosted_forbidden_fields():
        monkeypatch.delenv(name.upper(), raising=False)
    settings = Settings(_env_file=None, hosted_demo=True)
    assert settings.hosted_demo is True
    assert settings.payment_provider == "mock"
    assert settings.accounting_provider == "mock"
    assert settings.screening_provider == "fixture"
    # A normal demo on a laptop keeps the flag off and is not restricted by it.
    assert Settings(_env_file=None, circle_guard_address="0x" + "11" * 20).hosted_demo is False


def test_setup_reports_the_hosted_flag(tmp_path):
    (tmp_path / "hosted").mkdir()
    (tmp_path / "local").mkdir()
    hosted = _client(tmp_path / "hosted", hosted_demo=True).get("/setup").json()
    assert hosted["deployment"]["hosted"] is True
    assert hosted["deployment"]["mode"] == "demo"

    local = _client(tmp_path / "local").get("/setup").json()
    assert local["deployment"]["hosted"] is False


@pytest.fixture
def hosted_environment(monkeypatch):
    """Only HOSTED_DEMO is set; every other hosted-relevant variable starts absent."""
    names = [*hosted_forbidden_fields(), "payment_provider", "accounting_provider", "screening_provider"]
    for name in names:
        monkeypatch.delenv(name.upper(), raising=False)
    monkeypatch.setenv("HOSTED_DEMO", "1")
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("variable", "value", "message"),
    [
        ("PAYMENT_PROVIDER", "circle", "Hosted demo requires PAYMENT_PROVIDER=mock."),
        ("PAYMENT_PROVIDER", "local", "Hosted demo requires PAYMENT_PROVIDER=mock."),
        ("ACCOUNTING_PROVIDER", "frappe", "Hosted demo requires ACCOUNTING_PROVIDER=mock."),
        ("SCREENING_PROVIDER", "opensanctions", "Hosted demo requires SCREENING_PROVIDER=fixture."),
    ],
)
def test_startup_refuses_a_live_provider_and_never_prints_its_value(hosted_environment, variable, value, message):
    hosted_environment.setenv(variable, value)
    with pytest.raises(RuntimeError) as error:
        get_settings()

    assert str(error.value) == message
    assert value not in str(error.value)


@pytest.mark.parametrize("field", hosted_forbidden_fields())
def test_startup_refuses_a_live_credential_and_never_prints_its_value(hosted_environment, field):
    hosted_environment.setenv(field.upper(), SECRET)
    with pytest.raises(RuntimeError) as error:
        get_settings()

    assert str(error.value) == f"Hosted demo must not set {field.upper()}."
    assert SECRET not in str(error.value)


def test_the_startup_failure_exits_nonzero_without_echoing_the_credential():
    env = {key: value for key, value in os.environ.items() if key not in {name.upper() for name in hosted_forbidden_fields()}}
    env.update({"PYTHONPATH": str(ROOT / "src"), "HOSTED_DEMO": "1", "CIRCLE_API_KEY": SECRET,
                "PAYMENT_PROVIDER": "mock", "ACCOUNTING_PROVIDER": "mock", "SCREENING_PROVIDER": "fixture"})
    result = subprocess.run(
        [sys.executable, "-c", "from arc_payables.settings import get_settings; get_settings()"],
        capture_output=True, text=True, env=env, cwd=ROOT,
    )

    assert result.returncode != 0
    assert "CIRCLE_API_KEY" in result.stderr
    assert SECRET not in result.stderr and SECRET not in result.stdout


def test_the_hosted_deploy_files_state_the_mock_profile():
    deploy = ROOT / "deploy" / "hosted-demo"
    dockerfile = (deploy / "Dockerfile").read_text()
    entrypoint = (deploy / "entrypoint.sh").read_text()
    recipe = yaml.safe_load((deploy / "render.yaml").read_text())

    for setting in ("HOSTED_DEMO=1", "PAYMENT_PROVIDER=mock", "ACCOUNTING_PROVIDER=mock", "SCREENING_PROVIDER=fixture"):
        assert setting in dockerfile
    assert "DATABASE_PATH=/tmp" in dockerfile and "BACKUP_DIRECTORY=/tmp" in dockerfile
    assert "arc-payables-seed" in entrypoint and "exec uvicorn" in entrypoint

    service = recipe["services"][0]
    assert service["plan"] == "free" and service["healthCheckPath"] == "/health"
    assert service["runtime"] == "docker"
    # The only configured variable is the flag: a secret in this file would defeat the profile.
    assert service["envVars"] == [{"key": "HOSTED_DEMO", "value": "1"}]
