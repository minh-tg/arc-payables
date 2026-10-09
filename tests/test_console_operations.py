"""The operations the console can reach: reconciliation, backups, receivables and alert delivery.

Each one is a route the operator can press, so the tests cover what it returns, what it refuses, and
what it must never reveal: a webhook URL carries a credential in its path, and a backup directory is
a host layout that the console has no business echoing.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.auth import MUTATIONS
from arc_payables.ports import Receivable
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"
KEY = {"X-API-Key": "ops-key"}
WEBHOOK = "https://hooks.example.test/services/T000/B000/secret-token-that-must-not-leak"


def _app(tmp_path: Path, **overrides):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "ops.sqlite3",
        backup_directory=tmp_path / "backups",
        api_key="ops-key",
        min_reserve_usdc=Decimal("0"),
        **overrides,
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    app = create_app(settings=settings, store=store)
    return TestClient(app), app


def _receivable(external_id: str, amount_units: int, expected: date) -> Receivable:
    return Receivable(
        external_id=external_id,
        customer="Canteen Customer",
        reference=f"SINV-{external_id}",
        amount_units=amount_units,
        currency="USDC",
        expected_date=expected,
    )


# --------------------------------------------------------------------------------------
# Every call the console makes must be a route the API serves
# --------------------------------------------------------------------------------------


def _normalise(path: str) -> str:
    path = path.split("?", 1)[0]
    return re.sub(r"\{[^}]*\}", "{}", re.sub(r"\$\{[^}]*\}", "{}", path))


def test_every_path_the_console_calls_is_served_by_the_api(tmp_path):
    client, _ = _app(tmp_path)
    served = {_normalise(path) for path in client.get("/openapi.json").json()["paths"]}
    called = set()
    for module in WEB_DIR.glob("*.js"):
        for match in re.finditer(r"api\(\s*[`'\"]([^`'\"]+)[`'\"]", module.read_text()):
            path = match.group(1)
            if path.startswith("/auth/"):
                continue
            called.add((module.name, _normalise(path)))
    missing = sorted(f"{name}: {path}" for name, path in called if path not in served)
    assert not missing, f"the console calls routes the API does not serve: {missing}"


# --------------------------------------------------------------------------------------
# Reconciliation
# --------------------------------------------------------------------------------------


def test_reconciliation_reports_a_mock_deployment_as_nothing_to_compare(tmp_path):
    client, _ = _app(tmp_path)
    response = client.post("/reconciliation/run", headers=KEY)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["blocking"] == 0
    # A mock provider settles against the local database, so the honest answer is a review item, not a clean bill.
    assert [finding["kind"] for finding in body["findings"]] == ["provider_not_live"]
    assert body["findings"][0]["severity"] == "review"
    assert body["coverage"]["provider"] == "mock"


# --------------------------------------------------------------------------------------
# Backups
# --------------------------------------------------------------------------------------


def test_a_backup_is_listed_complete_and_restores_without_exposing_paths(tmp_path):
    client, _ = _app(tmp_path)
    assert client.get("/backups", headers=KEY).json() == {"backups": [], "count": 0}

    created = client.post("/backups", headers=KEY)
    assert created.status_code == 201
    name = created.json()["name"]
    assert name.startswith("arc-payables-") and name.endswith(".sqlite3")
    assert str(tmp_path) not in created.text

    assert created.json()["data_reaches"]
    listing = client.get("/backups", headers=KEY).json()
    assert listing["count"] == 1
    assert listing["backups"][0]["name"] == name
    assert listing["backups"][0]["complete"] is True
    assert listing["backups"][0]["sha256"]
    # A person reads "data reaches", so the answer is one timestamp, not the raw recovery-point structure.
    assert isinstance(listing["backups"][0]["data_reaches"], str)
    assert listing["backups"][0]["data_reaches"] == created.json()["data_reaches"]

    drill = client.post("/backups/restore-drill", headers=KEY, json={"backup": name})
    assert drill.status_code == 200, drill.text
    body = drill.json()
    assert body["ok"] is True
    assert body["name"] == name
    assert body["counts"]
    assert str(tmp_path) not in drill.text


def test_a_restore_drill_only_accepts_a_backup_the_listing_holds(tmp_path):
    client, _ = _app(tmp_path)
    client.post("/backups", headers=KEY)
    for attempt in ("does-not-exist.sqlite3", "../ops.sqlite3", "../../etc/passwd"):
        response = client.post("/backups/restore-drill", headers=KEY, json={"backup": attempt})
        assert response.status_code == 404, attempt
        assert response.json()["detail"]["code"] == "backup_not_found"


def test_a_backup_file_without_a_manifest_is_shown_as_incomplete_not_hidden(tmp_path):
    client, _ = _app(tmp_path)
    folder = tmp_path / "backups"
    folder.mkdir(parents=True)
    (folder / "arc-payables-20260101T000000Z.sqlite3").write_bytes(b"not a database")
    listing = client.get("/backups", headers=KEY).json()
    assert listing["count"] == 1
    assert listing["backups"][0]["complete"] is False
    refused = client.post(
        "/backups/restore-drill", headers=KEY, json={"backup": "arc-payables-20260101T000000Z.sqlite3"}
    )
    assert refused.status_code == 409


# --------------------------------------------------------------------------------------
# Receivables
# --------------------------------------------------------------------------------------


def test_receivables_sync_is_idempotent_and_collection_stops_counting_them(tmp_path, monkeypatch):
    client, app = _app(tmp_path)
    accounting = app.state.workflow.accounting
    monkeypatch.setattr(
        accounting,
        "list_receivables",
        lambda: [_receivable("SINV-1", 2_000_000, date(2026, 10, 20)), _receivable("SINV-2", 500_000, date(2026, 10, 25))],
    )

    assert client.get("/receivables", headers=KEY).json()["count"] == 0
    assert client.post("/receivables/sync", headers=KEY).json() == {"recorded": 2}
    assert client.post("/receivables/sync", headers=KEY).json() == {"recorded": 2}

    rows = client.get("/receivables", headers=KEY).json()
    assert rows["count"] == 2
    assert rows["receivables"][0]["external_id"] == "SINV-1"
    assert rows["receivables"][0]["amount_usdc"] == 2.0

    assert client.post("/receivables/SINV-1/collect", headers=KEY).json() == {"collected": "SINV-1"}
    assert [row["external_id"] for row in client.get("/receivables", headers=KEY).json()["receivables"]] == ["SINV-2"]
    assert client.post("/receivables/SINV-1/collect", headers=KEY).status_code == 404


def test_an_unreadable_accounting_system_records_nothing(tmp_path, monkeypatch):
    client, app = _app(tmp_path)

    def unreachable():
        raise RuntimeError("connection refused")

    monkeypatch.setattr(app.state.workflow.accounting, "list_receivables", unreachable)
    response = client.post("/receivables/sync", headers=KEY)
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "accounting_unreadable"
    assert client.get("/receivables", headers=KEY).json()["count"] == 0


# --------------------------------------------------------------------------------------
# Alert delivery
# --------------------------------------------------------------------------------------


def test_a_test_alert_refuses_clearly_when_no_destination_is_configured(tmp_path):
    client, _ = _app(tmp_path)
    response = client.post("/alerts/test", headers=KEY)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "alert_destination_missing"


def test_a_delivered_test_alert_names_only_the_host_of_its_destination(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, alert_webhook_url=WEBHOOK)
    monkeypatch.setattr("arc_payables.alerting.WebhookSink.send", lambda self, alerts: [])
    response = client.post("/alerts/test", headers=KEY)
    assert response.status_code == 200
    assert response.json() == {"delivered": True, "destination": "hooks.example.test"}
    assert "secret-token" not in response.text


def test_a_failed_test_alert_reports_the_failure_without_the_credential(tmp_path, monkeypatch):
    client, _ = _app(tmp_path, alert_webhook_url=WEBHOOK)
    monkeypatch.setattr(
        "arc_payables.alerting.WebhookSink.send",
        lambda self, alerts: [{"error": f"HTTP 500 from {WEBHOOK}"}],
    )
    response = client.post("/alerts/test", headers=KEY)
    body = response.json()
    assert body["delivered"] is False
    assert body["destination"] == "hooks.example.test"
    assert "error" in body and "secret-token" not in response.text


# --------------------------------------------------------------------------------------
# Who may press them
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "route",
    [
        "run_reconciliation",
        "create_backup_now",
        "run_restore_drill",
        "sync_receivables",
        "collect_receivable",
        "send_test_alert",
    ],
)
def test_each_new_action_requires_the_operations_role(route):
    assert MUTATIONS[route] == "operate"
