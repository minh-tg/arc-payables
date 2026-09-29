"""Continuous re-screening and risk-tiered limits.

The assertions that matter: a risk change is recorded rather than silently absorbed, a
downgrade actually reduces what may be paid unattended, and the default posture (a human when
screening is unclear) is untouched unless the operator explicitly opts into reduced limits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from test_prioritisation import _add_invoice, _workspace

from tameion.domain import ScreeningStatus, USDC_SCALE, WorkflowState
from tameion.monitoring import rescreen_suppliers, supplier_risk_overview
from tameion.screening import FixtureScreeningProvider
from tameion.seed import APPROVED_WALLET, seed_demo
from tameion.store import SQLiteEvidenceStore

SUPPLIER = "SUP-ACME-001"


def _screener_for(store, status: ScreeningStatus) -> FixtureScreeningProvider:
    """A provider whose answer can be changed between checks, like a real list can."""
    provider = FixtureScreeningProvider(store, default_status=status)
    return provider


def _set_fixture(store, status: ScreeningStatus) -> None:
    store.seed_fixture("screening", APPROVED_WALLET.lower(), {"status": status.value})


def test_the_first_check_is_recorded_and_reported(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)

    outcomes = rescreen_suppliers(workflow, source="test")

    assert outcomes
    outcome = next(item for item in outcomes if item.supplier_id == SUPPLIER)
    assert outcome.direction == "first_check"
    assert outcome.status == "CLEAR" and outcome.tier == "low"
    assert outcome.rechecked is True
    assert store.latest_screening(SUPPLIER)["status"] == "CLEAR"


def test_a_not_due_counterparty_is_not_re_screened(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)
    rescreen_suppliers(workflow, source="test")

    again = rescreen_suppliers(workflow, source="test")
    outcome = next(item for item in again if item.supplier_id == SUPPLIER)
    assert outcome.rechecked is False
    assert "Not due" in outcome.detail

    forced = rescreen_suppliers(workflow, force=True, source="test")
    outcome = next(item for item in forced if item.supplier_id == SUPPLIER)
    assert outcome.rechecked is True
    assert store.latest_screening(SUPPLIER) is not None
    assert len(store.screenings(SUPPLIER)) == 2


def test_a_downgrade_is_recorded_on_every_open_invoice_chain(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)
    rescreen_suppliers(workflow, source="test")
    invoice = _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="RS-OPEN")

    _set_fixture(store, ScreeningStatus.INCONCLUSIVE)
    outcomes = rescreen_suppliers(workflow, force=True, source="test")
    outcome = next(item for item in outcomes if item.supplier_id == SUPPLIER)

    assert outcome.direction == "downgrade"
    assert outcome.previous_status == "CLEAR" and outcome.status == "INCONCLUSIVE"
    assert outcome.invoices_annotated >= 1

    events = [event["type"] for event in workflow.events(invoice.id)]
    assert "SUPPLIER_RISK_CHANGED" in events
    event = next(item for item in workflow.events(invoice.id) if item["type"] == "SUPPLIER_RISK_CHANGED")
    assert event["payload"]["direction"] == "downgrade"
    assert event["payload"]["previous_status"] == "CLEAR"
    # The risk change is part of the tamper-evident chain, which still verifies.
    assert store.verify_audit_chain()["ok"] is True


def test_the_default_posture_still_requires_a_human_when_screening_is_unclear(tmp_path):
    """Opting out is the default: an inconclusive result must not become payable."""
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)
    invoice = _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="RS-STRICT")
    assert workflow.evaluate(invoice.id)["state"] == WorkflowState.ELIGIBLE.value

    _set_fixture(store, ScreeningStatus.INCONCLUSIVE)
    rescreen_suppliers(workflow, force=True, source="test")
    reevaluated = workflow.evaluate(invoice.id)

    assert reevaluated["state"] == WorkflowState.ESCALATED.value
    check = next(item for item in reevaluated["decision"]["policy_checks"] if item["code"] == "screening_ambiguous")
    assert check["requires_human"] is True


def test_reduced_limits_let_a_medium_risk_counterparty_be_paid_less(tmp_path):
    """With reduced-limit handling on, risk tiers scale the limit instead of blocking."""
    # A 400 USDC limit means a medium-risk counterparty may spend 100 unattended: the 200 USDC
    # invoice is blocked, the 80 USDC one is not. Amounts stay inside the demo purchase order.
    _, store, workflow, _ = _workspace(
        tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"),
        max_invoice_usdc=Decimal("400"), screening_medium_tier_handling="limit",
    )
    _set_fixture(store, ScreeningStatus.CLEAR)
    large = _add_invoice(workflow, store, amount=200 * USDC_SCALE, due_in_days=0, number="RS-LARGE")
    small = _add_invoice(workflow, store, amount=80 * USDC_SCALE, due_in_days=0, number="RS-SMALL")
    assert workflow.evaluate(large.id)["state"] == WorkflowState.ELIGIBLE.value
    assert workflow.evaluate(small.id)["state"] == WorkflowState.ELIGIBLE.value

    _set_fixture(store, ScreeningStatus.INCONCLUSIVE)
    rescreen_suppliers(workflow, force=True, source="test")

    # The smaller obligation is still payable unattended; the larger one now needs a human.
    # That is a reduced limit, not a refusal.
    reevaluated_small = workflow.evaluate(small.id)
    assert reevaluated_small["state"] == WorkflowState.ELIGIBLE.value
    limit = next(item for item in reevaluated_small["decision"]["policy_checks"] if item["code"] == "amount_limit")
    assert "reduced 100 USDC" in limit["detail"]

    reevaluated_large = workflow.evaluate(large.id)
    assert reevaluated_large["state"] == WorkflowState.ESCALATED.value
    large_limit = next(item for item in reevaluated_large["decision"]["policy_checks"] if item["code"] == "amount_limit")
    assert large_limit["passed"] is False


def test_a_flagged_counterparty_still_needs_a_human_even_with_reduced_limits(tmp_path):
    _, store, workflow, _ = _workspace(
        tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"),
        screening_medium_tier_handling="limit",
    )
    _set_fixture(store, ScreeningStatus.CLEAR)
    invoice = _add_invoice(workflow, store, amount=10 * USDC_SCALE, due_in_days=0, number="RS-FLAG")
    assert workflow.evaluate(invoice.id)["state"] == WorkflowState.ELIGIBLE.value

    _set_fixture(store, ScreeningStatus.FLAGGED)
    rescreen_suppliers(workflow, force=True, source="test")
    reevaluated = workflow.evaluate(invoice.id)

    assert reevaluated["state"] == WorkflowState.ESCALATED.value
    check = next(item for item in reevaluated["decision"]["policy_checks"] if item["code"] == "screening_flagged")
    assert check["requires_human"] is True and check["overridable"] is False


def test_an_unavailable_provider_is_recorded_and_does_not_clear_anyone(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)
    rescreen_suppliers(workflow, source="test")

    workflow.screener = _screener_for(store, ScreeningStatus.UNAVAILABLE)
    store.seed_fixture("screening", APPROVED_WALLET.lower(), {"status": "UNAVAILABLE"})
    outcomes = rescreen_suppliers(workflow, force=True, source="test")
    outcome = next(item for item in outcomes if item.supplier_id == SUPPLIER)

    assert outcome.status == "UNAVAILABLE" and outcome.tier == "medium"
    assert store.latest_screening(SUPPLIER)["status"] == "UNAVAILABLE"
    assert outcome.provider  # the provider that produced the result is recorded


def test_the_risk_overview_reports_tier_limit_and_open_exposure(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=10_000 * USDC_SCALE, reserve=Decimal("0"))
    _set_fixture(store, ScreeningStatus.CLEAR)
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="RS-OVERVIEW")
    rescreen_suppliers(workflow, source="test")

    overview = supplier_risk_overview(workflow)

    entry = next(item for item in overview if item["supplier_id"] == SUPPLIER)
    assert entry["risk_tier"] == "low"
    assert Decimal(entry["automatic_limit_usdc"]) == Decimal("1000")
    assert entry["open_invoices"] >= 1
    assert Decimal(entry["open_amount_usdc"]) > 0


def test_the_endpoints_require_a_key_and_report_risk(tmp_path):
    from fastapi.testclient import TestClient

    from tameion.api import create_app
    from tameion.settings import Settings

    settings = Settings(
        _env_file=None, database_path=tmp_path / "api-risk.sqlite3", api_key="k", min_reserve_usdc=Decimal("0")
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    app = create_app(settings=settings, store=store)
    client = TestClient(app)
    assert client.get("/suppliers").status_code == 401
    assert client.post("/monitoring/rescreen").status_code == 401

    rescreen = client.post("/monitoring/rescreen?force=true", headers={"X-API-Key": "k"})
    assert rescreen.status_code == 200
    assert isinstance(rescreen.json(), list)

    suppliers = client.get("/suppliers", headers={"X-API-Key": "k"}).json()
    assert suppliers and {"supplier_id", "risk_tier", "automatic_limit_usdc", "open_invoices"} <= set(suppliers[0])
