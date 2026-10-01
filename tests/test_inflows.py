"""Inflows: expected money in, so the forecast answers coverage, not just spend.

The forecast walked obligations against the balance alone. A receivable arriving before a
due date changes whether the balance covers it, so ignoring inflows understates coverage
and invents shortfalls that will not happen. These tests pin the inflow walk: money in
arrives on its expected date, never authorizes anything, and never hides money owed.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from test_prioritisation import _add_invoice, _workspace

from arc_payables.domain import USDC_SCALE, utcnow
from arc_payables.forecast import build_forecast


def _receivable(store, *, external_id: str, amount_units: int, expected_in_days: int, customer: str = "ACME-BUYER"):
    return store.record_receivable({
        "external_id": external_id,
        "customer": customer,
        "reference": f"SO-{external_id}",
        "amount_units": amount_units,
        "currency": "USDC",
        "expected_date": (date.today() + timedelta(days=expected_in_days)).isoformat(),
        "source": "test",
    })


def test_an_inflow_before_the_due_date_covers_an_otherwise_uncovered_obligation(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=100 * USDC_SCALE, reserve=Decimal("0"))
    for invoice, _state in store.list_invoices():
        if invoice.id.startswith("demo-invoice"):
            store.set_state(invoice.id, "ERP_RECORDED", "TEST_SETTLED", {})
    _add_invoice(workflow, store, amount=250 * USDC_SCALE, due_in_days=5, number="IN-DUE")
    before = build_forecast(workflow, days=30)
    assert before.shortfall is True

    _receivable(store, external_id="SINV-001", amount_units=200 * USDC_SCALE, expected_in_days=2)
    after = build_forecast(workflow, days=30)

    assert after.inflow_total_units == 200 * USDC_SCALE
    assert after.shortfall is False
    assert all(item.coverable for item in after.obligations)


def test_an_inflow_after_the_due_date_does_not_cover_it(tmp_path):
    """Money arriving late is not money available: timing is the whole point of the walk."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=100 * USDC_SCALE, reserve=Decimal("0"))
    for invoice, _state in store.list_invoices():
        if invoice.id.startswith("demo-invoice"):
            store.set_state(invoice.id, "ERP_RECORDED", "TEST_SETTLED", {})
    _add_invoice(workflow, store, amount=250 * USDC_SCALE, due_in_days=5, number="IN-LATE")

    _receivable(store, external_id="SINV-002", amount_units=500 * USDC_SCALE, expected_in_days=9)
    forecast = build_forecast(workflow, days=30)

    assert [item.invoice_number for item in forecast.obligations] == ["IN-LATE"]
    assert forecast.shortfall is True
    assert forecast.shortfall_date == date.today() + timedelta(days=5)


def test_inflows_beyond_the_horizon_are_excluded(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _receivable(store, external_id="SINV-003", amount_units=400 * USDC_SCALE, expected_in_days=60)

    forecast = build_forecast(workflow, days=30)

    assert forecast.inflow_total_units == 0
    assert forecast.inflows == ()


def test_reingestion_replaces_rather_than_duplicates_a_receivable(tmp_path):
    """ERPNext is re-read, not appended to: the external id is the identity."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _receivable(store, external_id="SINV-004", amount_units=100 * USDC_SCALE, expected_in_days=2)
    _receivable(store, external_id="SINV-004", amount_units=300 * USDC_SCALE, expected_in_days=2)

    forecast = build_forecast(workflow, days=30)

    assert forecast.inflow_total_units == 300 * USDC_SCALE
    assert [item.external_id for item in forecast.inflows] == ["SINV-004"]


def test_a_collected_receivable_leaves_the_forecast(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _receivable(store, external_id="SINV-005", amount_units=100 * USDC_SCALE, expected_in_days=2)
    assert build_forecast(workflow, days=30).inflow_total_units == 100 * USDC_SCALE

    assert store.mark_receivable_collected("SINV-005", utcnow().isoformat()) is True
    assert build_forecast(workflow, days=30).inflow_total_units == 0


def test_a_broken_connector_does_not_break_the_forecast(tmp_path, monkeypatch):
    """Inflows are additive context. A connector that cannot list them degrades to the old view."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))

    def broken(self):
        raise RuntimeError("receivables source is down")

    monkeypatch.setattr(type(workflow.accounting), "list_receivables", broken)
    forecast = build_forecast(workflow, days=30)

    assert forecast.inflows == ()
    assert forecast.inflow_total_units == 0


def test_the_forecast_endpoint_reports_inflows(tmp_path):
    from fastapi.testclient import TestClient

    from arc_payables.api import create_app
    from arc_payables.settings import Settings

    settings = Settings(
        _env_file=None, database_path=tmp_path / "api-inflows.sqlite3", api_key="k", min_reserve_usdc=Decimal("0")
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    app.state.store.record_receivable({
        "external_id": "SINV-API",
        "customer": "API-BUYER",
        "reference": "SO-API",
        "amount_units": 50 * USDC_SCALE,
        "currency": "USDC",
        "expected_date": date.today().isoformat(),
        "source": "test",
    })
    body = client.get("/forecast", headers={"X-API-Key": "k"}).json()
    assert body["inflow_within_horizon_usdc"] == "50"
    assert [item["external_id"] for item in body["inflows"]] == ["SINV-API"]
