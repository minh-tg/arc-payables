"""Forward coverage: does the treasury cover what is actually coming?

These reuse the plan's test helpers on purpose: the forecast and the payment plan must not
disagree about what is affordable, so they share one reserve-floor rule and one set of
fixtures.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from test_prioritisation import _add_invoice, _workspace

from tameion.domain import USDC_SCALE
from tameion.forecast import build_forecast
from tameion.settings import Settings


def _settle_seed(store):
    """Park the seeded demo invoices so a forecast covers only this test's obligations.

    The seed data is real (and the suspicious invoice is genuinely owed), so a test that wants
    to reason about a specific set of obligations has to retire it first rather than pretend it
    is not there.
    """
    for invoice, _state in store.list_invoices():
        if invoice.id.startswith("demo-invoice"):
            store.set_state(invoice.id, "ERP_RECORDED", "TEST_SETTLED", {"reason": "test isolation"})


def test_obligations_are_ordered_by_due_date_with_reasons(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=5, number="FC-LATER")
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=-3, number="FC-OVERDUE")
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="FC-TODAY")

    forecast = build_forecast(workflow, days=30)

    numbers = [item.invoice_number for item in forecast.obligations]
    assert numbers.index("FC-OVERDUE") < numbers.index("FC-TODAY") < numbers.index("FC-LATER")
    overdue = next(item for item in forecast.obligations if item.invoice_number == "FC-OVERDUE")
    assert "overdue" in overdue.reasons and overdue.days_until_due < 0
    assert forecast.shortfall is False


def test_the_shortfall_date_is_the_first_obligation_the_balance_cannot_cover(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=300 * USDC_SCALE, reserve=Decimal("0"))
    _settle_seed(store)
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=1, number="FC-FIRST")
    _add_invoice(workflow, store, amount=250 * USDC_SCALE, due_in_days=6, number="FC-SECOND")

    forecast = build_forecast(workflow, days=30)

    assert forecast.shortfall is True
    assert forecast.shortfall_date == date.today() + timedelta(days=6)
    assert "FC-SECOND" in [item.invoice_number for item in forecast.obligations if not item.coverable]
    covered = next(item for item in forecast.obligations if item.invoice_number == "FC-FIRST")
    assert covered.coverable is True
    assert covered.projected_balance_units == 200 * USDC_SCALE
    uncovered = next(item for item in forecast.obligations if item.invoice_number == "FC-SECOND")
    assert uncovered.projected_balance_units is None
    assert Decimal(forecast.to_dict()["shortfall_usdc"]) == Decimal("250")


def test_the_reserve_floor_is_respected_exactly_as_the_plan_does(tmp_path):
    """A balance that covers the same set of invoices under both rules."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("300"))
    _settle_seed(store)
    _add_invoice(workflow, store, amount=700 * USDC_SCALE, due_in_days=0, number="FC-BIG")
    _add_invoice(workflow, store, amount=700 * USDC_SCALE, due_in_days=1, number="FC-BIG2")

    forecast = build_forecast(workflow, days=30)

    # 1000 - 700 = 300, which is the floor, so the first is covered and the second is not.
    assert forecast.obligations[0].invoice_number in {"FC-BIG", "FC-BIG2"}
    assert sum(1 for item in forecast.obligations if item.coverable) == 1
    assert forecast.shortfall is True


def test_a_settled_invoice_is_no_longer_an_obligation(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _settle_seed(store)
    invoice = _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="FC-PAID")
    before = build_forecast(workflow, days=30)
    assert invoice.id in [item.invoice_id for item in before.obligations]

    workflow.evaluate(invoice.id)
    result = workflow.submit_payment(invoice.id)
    assert result["state"] == "ERP_RECORDED"

    after = build_forecast(workflow, days=30)
    assert invoice.id not in [item.invoice_id for item in after.obligations]


def test_a_blocked_invoice_is_still_counted_as_money_owed(tmp_path):
    """Evidence gaps do not cancel a debt, so the forecast must not hide it."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="FC-OK")

    forecast = build_forecast(workflow, days=30)

    blocked = [item for item in forecast.obligations if not item.payable_by_agent]
    assert blocked, "the seeded suspicious invoice should be an obligation the agent may not pay"
    assert all(item.not_payable_reason for item in blocked)
    assert all(item.amount_units > 0 for item in blocked)
    assert any("not payable by the agent yet" in note for note in forecast.notes)


def test_an_expiring_discount_is_valued(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(
        workflow, store, amount=1_000 * USDC_SCALE, due_in_days=10, number="FC-DISC",
        discount_percent="2.00", discount_in_days=2,
    )

    forecast = build_forecast(workflow, days=30)

    discounted = next(item for item in forecast.obligations if item.invoice_number == "FC-DISC")
    assert discounted.discount_value_units == 20 * USDC_SCALE
    assert discounted.discount_deadline == date.today() + timedelta(days=2)
    assert "early_payment_discount_expiring" in discounted.reasons


def test_obligations_beyond_the_horizon_are_reported_not_hidden(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="FC-NOW")
    _add_invoice(workflow, store, amount=400 * USDC_SCALE, due_in_days=60, number="FC-FAR")

    forecast = build_forecast(workflow, days=30)

    assert "FC-FAR" not in [item.invoice_number for item in forecast.obligations]
    assert forecast.beyond_horizon_units >= 400 * USDC_SCALE
    assert any("beyond the 30 day horizon" in note for note in forecast.notes)


def test_the_forecast_endpoint_requires_a_key_and_reports_coverage(tmp_path):
    from fastapi.testclient import TestClient

    from tameion.api import create_app

    settings = Settings(
        _env_file=None, database_path=tmp_path / "api-forecast.sqlite3", api_key="k", min_reserve_usdc=Decimal("0")
    )
    app = create_app(settings=settings)
    client = TestClient(app)
    assert client.get("/forecast").status_code == 401
    body = client.get("/forecast?days=45", headers={"X-API-Key": "k"}).json()
    assert body["horizon_days"] == 45
    assert {"balance_usdc", "due_within_horizon_usdc", "shortfall", "obligations", "rationale"} <= set(body)
    assert Decimal(body["coverable_usdc"]) <= Decimal(body["balance_usdc"])
