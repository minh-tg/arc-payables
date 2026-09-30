"""The metrics endpoint.

Two things matter here. The numbers have to be right, including when a provider is unreachable, and
the endpoint has to be closed by default: it exposes the treasury balance and the payment history,
which is not public information.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from arc_payables.domain import WorkflowState, utcnow
from arc_payables.metrics import collect, render
from arc_payables.settings import Settings

METRIC_PREFIX = "arc_payables_"


def _client_with_key(runtime) -> tuple[TestClient, str]:
    key = "metrics-test-key"
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


def test_metrics_requires_the_scrape_credential(runtime):
    client, key = _client_with_key(runtime)
    assert client.get("/metrics").status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/metrics", headers={"X-API-Key": key}).status_code == 200
    # Prometheus cannot send X-API-Key, so a bearer token is accepted as well.
    response = client.get("/metrics", headers={"Authorization": f"Bearer {key}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")


def test_metrics_reports_invoice_and_payment_state(runtime):
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])

    body = render(collect(runtime["workflow"]))
    assert f'{METRIC_PREFIX}invoices{{state="ERP_RECORDED"}} 1' in body
    assert f'{METRIC_PREFIX}payments{{confirmation="CONFIRMED"}} 1' in body
    assert f'{METRIC_PREFIX}payments_erp_status{{status="RECORDED"}} 1' in body
    assert f'{METRIC_PREFIX}audit_chain_ok 1' in body
    assert f'{METRIC_PREFIX}payments_needing_attention{{reason="unrecorded"}} 0' in body
    assert "# TYPE arc_payables_invoices gauge" in body
    # A metric family must be declared once, however many samples it has.
    assert body.count("# HELP arc_payables_payments_needing_attention") == 1


def test_metrics_flags_a_settlement_that_was_never_confirmed(runtime):
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])
    runtime["store"].update_payment(
        runtime["legitimate_id"],
        {"confirmation_status": "UNCERTAIN", "erp_status": "PENDING"},
        WorkflowState.NEEDS_RECONCILIATION.value,
        "TEST_SETUP",
    )
    snapshot = collect(runtime["workflow"])
    assert snapshot["payments_uncertain"] == 1
    assert snapshot["payments_awaiting_ledger"] == 0
    body = render(snapshot)
    assert f'{METRIC_PREFIX}payments_needing_attention{{reason="unconfirmed"}} 1' in body


def test_metrics_reports_the_reserve_and_the_headroom(runtime):
    snapshot = collect(runtime["workflow"])
    # The demo runtime holds 5,000 USDC against the default 2,000 USDC floor.
    assert snapshot["treasury_usdc"] == 5000
    assert snapshot["reserve_headroom_usdc"] == 3000
    body = render(snapshot)
    assert f"{METRIC_PREFIX}reserve_headroom_usdc 3000.0" in body
    # The mock provider has no guard to read, so no guard metric is invented.
    assert "arc_payables_guard_limit_usdc" not in body


def test_metrics_survive_an_unreachable_provider(runtime, monkeypatch):
    """A scraper must still get numbers when the chain is down, and one of them says so."""

    def unreachable():
        raise RuntimeError("rpc is down")

    monkeypatch.setattr(runtime["payment"], "get_balance", unreachable)
    snapshot = collect(runtime["workflow"])
    assert snapshot["treasury_available"] is False
    assert snapshot["reserve_headroom_usdc"] is None
    body = render(snapshot)
    assert f"{METRIC_PREFIX}treasury_probe_ok 0" in body
    assert f"{METRIC_PREFIX}treasury_usdc 0.0" in body
    assert "arc_payables_reserve_headroom_usdc" not in body


def test_metrics_reports_worker_health_from_another_process(runtime):
    """The worker writes its passes down precisely so a metrics scrape can see them."""
    store = runtime["store"]
    assert collect(runtime["workflow"])["worker_last_run_timestamp"] is None

    now = utcnow().isoformat()
    store.record_worker_run(now, now, "ok", {})
    store.record_worker_run(now, now, "degraded", {})

    snapshot = collect(runtime["workflow"])
    assert snapshot["worker_consecutive_failures"] == 1
    assert snapshot["worker_last_outcome"] == "degraded"
    body = render(snapshot)
    assert f"{METRIC_PREFIX}worker_consecutive_failures 1" in body
    assert f'{METRIC_PREFIX}worker_runs{{outcome="degraded"}} 1' in body
    assert f'{METRIC_PREFIX}worker_last_run_timestamp_seconds{{outcome="degraded"}}' in body


def test_screenings_that_are_due_are_counted(runtime):
    snapshot = collect(runtime["workflow"])
    # The demo seed records no screening history for the supplier, only a fixture status.
    assert snapshot["screenings_due"] >= 1
    assert f"{METRIC_PREFIX}screenings_due {snapshot['screenings_due']}" in render(snapshot)


def test_metric_labels_are_escaped(runtime):
    store = runtime["store"]
    store.record_worker_run(utcnow().isoformat(), utcnow().isoformat(), 'weird"outcome', {})
    body = render(collect(runtime["workflow"]))
    assert 'outcome="weird\\"outcome"' in body
