"""The worker status endpoint and its console panel.

Pass history and alerts are stored by a separate process, so the only way the console can show
them is an endpoint that reads the same records. These tests check the endpoint is closed like the
rest of the API, reports what the store knows, and never invents alerts it did not record.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from arc_payables import worker
from arc_payables.api import create_app
from arc_payables.domain import utcnow
from arc_payables.settings import Settings

WEB_DIR = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"


def _client_with_key(runtime) -> tuple[TestClient, str]:
    key = "worker-status-key"
    settings = Settings(
        _env_file=None,
        database_path=runtime["settings"].database_path,
        api_key=key,
    )
    from arc_payables.api import create_app

    app = create_app(
        settings=settings,
        store=runtime["store"],
        accounting=runtime["accounting"],
        payment_provider=runtime["payment"],
    )
    return TestClient(app), key


def test_worker_status_requires_the_api_key(runtime):
    client, key = _client_with_key(runtime)
    assert client.get("/worker/status").status_code == 401
    assert client.get("/worker/status", headers={"X-API-Key": "wrong"}).status_code == 401
    response = client.get("/worker/status", headers={"X-API-Key": key})
    assert response.status_code == 200


def test_worker_status_reports_no_pass_yet(runtime):
    client, key = _client_with_key(runtime)
    body = client.get("/worker/status", headers={"X-API-Key": key}).json()
    assert body["last"] is None
    assert body["consecutive_failures"] == 0
    assert body["observed"] == 0
    assert body["alerts"] == []
    assert body["alert_delivery"] == []


def test_worker_status_returns_the_summary_plus_the_latest_alerts(runtime):
    report = worker.run_pass(runtime["workflow"])
    assert report.outcome == "ok"

    client, key = _client_with_key(runtime)
    body = client.get("/worker/status", headers={"X-API-Key": key}).json()
    summary = runtime["store"].worker_summary()
    assert body["last"]["outcome"] == summary["last"]["outcome"]
    assert body["consecutive_failures"] == summary["consecutive_failures"]
    assert body["outcomes"] == summary["outcomes"]
    assert body["observed"] == summary["observed"]
    # The pass the worker just ran raised no alerts, and the endpoint repeats that, not a fresh view.
    assert body["alerts"] == report.alerts


def test_worker_status_repeats_stored_alerts_without_recomputing(runtime):
    now = utcnow().isoformat()
    stored = [{"code": "reserve_breached", "severity": "critical", "summary": "below the floor", "detail": {}}]
    runtime["store"].record_worker_run(
        now,
        now,
        "degraded",
        {"outcome": "degraded", "alerts": stored, "alert_delivery": [{"code": "reserve_breached", "error": "Timeout: down"}]},
    )

    client, key = _client_with_key(runtime)
    body = client.get("/worker/status", headers={"X-API-Key": key}).json()
    assert body["alerts"] == stored
    assert body["alert_delivery"] == [{"code": "reserve_breached", "error": "Timeout: down"}]
    assert body["last"]["outcome"] == "degraded"


def test_worker_status_is_documented(runtime):
    spec = runtime["client"].get("/openapi.json").json()
    assert "/worker/status" in spec["paths"]


def test_the_console_links_the_worker_view():
    shell = (WEB_DIR / "index.html").read_text()
    assert "#/worker" in shell
    assert "./worker.js" in shell or "'./worker.js'" in shell or '"./worker.js"' in shell
    assert (WEB_DIR / "worker.js").is_file()
