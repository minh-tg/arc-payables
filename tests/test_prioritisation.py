"""Cross-invoice prioritisation: what to pay first when the balance cannot cover everything.

The load-bearing assertions are about the boundary between advice and money. A planner may
reorder the queue; it may not add an invoice, drop the reserve floor, or spend more than the
balance allows, and any unusable answer leaves the deterministic order untouched.
"""

from __future__ import annotations

import importlib.util
import json
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from arc_payables.currency import USDCOnlyConverter
from arc_payables.deliberation import DeliberatingPlanner, build_order_planner
from arc_payables.domain import DecisionAction, USDC_SCALE
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.prioritisation import EXCLUDED_BELOW_RESERVE, EXCLUDED_NOT_ELIGIBLE, PaymentPrioritiser, plan_digest
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

ROOT = Path(__file__).resolve().parents[1]


def _demo_module():
    """Load script/live-planner-demo.py as a module so its verdict logic can be tested."""
    spec = importlib.util.spec_from_file_location("live_planner_demo", ROOT / "script/live-planner-demo.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _workspace(tmp_path: Path, *, balance_units: int = 2_500 * USDC_SCALE, reserve: Decimal = Decimal("2000"), **overrides):
    settings = Settings(_env_file=None, database_path=tmp_path / "plan.sqlite3", min_reserve_usdc=reserve, **overrides)
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    seed_demo(store)
    provider = MockPaymentProvider(store, balance_units=balance_units)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        provider.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return settings, store, workflow, provider


def _add_invoice(workflow, store, *, amount: int, due_in_days: int, number: str, discount_percent: str | None = None, discount_in_days: int | None = None):
    """A payable invoice with a matching accounting payable, so it is genuinely eligible.

    The amount change is applied through the same helper the seed data uses, so the invoice
    lines, the lines' amounts and the linked order/receipt references stay consistent and the
    policy's line-total and three-way-match checks still pass.
    """
    from arc_payables.seed import _erp_payable, _replace_amount

    # The legitimate seed invoice: it carries the trusted payee and real order/receipt links.
    base = store.get_invoice("demo-invoice-legitimate")
    if base is None:
        base = next(invoice for invoice, _state in store.list_invoices() if invoice.purchase_invoice_id)
    today = date.today()
    scaled = _replace_amount(base, amount, base.lines[0].quantity)
    invoice = replace(
        scaled,
        id=f"plan-{number}",
        invoice_number=number,
        purchase_invoice_id=f"PINV-{number}",
        due_date=today + timedelta(days=due_in_days),
        discount_percent=discount_percent,
        discount_deadline=(today + timedelta(days=discount_in_days)) if discount_in_days is not None else None,
    )
    store.create_invoice(invoice, f"key-{number}")
    store.seed_fixture(
        "erp_payable",
        invoice.purchase_invoice_id,
        _erp_payable(
            external_id=invoice.purchase_invoice_id,
            invoice_number=number,
            amount_units=amount,
            invoice_currency="USD",
            invoice_date=invoice.invoice_date,
            due_date=invoice.due_date,
            lines=invoice.lines,
            purchase_order_ids=invoice.purchase_order_ids,
            receipt_ids=invoice.receipt_ids,
            payment_terms=invoice.payment_terms,
            status="SUBMITTED",
        ),
    )
    return invoice


def _planner_settings() -> Settings:
    """Planner-only settings: a configured endpoint is what makes a planner callable at all."""
    return Settings(
        _env_file=None,
        decision_layer="dual_process",
        planner_base_url="https://planner.test/v1",
        planner_model="test-model",
    )


def _planner(settings, reply, *, status_code: int = 200, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(json.loads(request.content))
        if status_code >= 400:
            return httpx.Response(status_code, json={"error": "nope"})
        content = reply if isinstance(reply, str) else json.dumps(reply)
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    return DeliberatingPlanner(_planner_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))


# --------------------------------------------------------------------------------------
# The deterministic part: what is affordable
# --------------------------------------------------------------------------------------


def test_a_plan_never_spends_below_the_reserve_floor(tmp_path):
    """Both invoices pass their own checks; the treasury can fund only one of them.

    This is the question individual evaluation cannot answer, so it is the one the plan exists
    for: each invoice is affordable on its own, and only the sequence reveals that the second
    would breach the reserve.
    """
    _, store, workflow, _ = _workspace(tmp_path, balance_units=2_100 * USDC_SCALE, reserve=Decimal("2000"))
    _add_invoice(workflow, store, amount=60 * USDC_SCALE, due_in_days=0, number="PLAN-A")
    _add_invoice(workflow, store, amount=60 * USDC_SCALE, due_in_days=0, number="PLAN-B")

    prioritiser = PaymentPrioritiser(workflow)
    individually_payable = [item for item in prioritiser.candidates() if item["eligible"]]
    assert len(individually_payable) == 2, "precondition: both invoices pass their own checks"

    plan = prioritiser.plan()

    assert len(plan.ordered) == 1, "only one of the two is actually affordable"
    assert Decimal(plan.planned_spend_usdc) <= Decimal(plan.spendable_usdc)
    assert Decimal(plan.balance_usdc) - Decimal(plan.planned_spend_usdc) >= Decimal(plan.reserve_floor_usdc)
    # The unaffordable invoices are reported with the reason, not silently dropped.
    assert any(EXCLUDED_BELOW_RESERVE in entry.reasons for entry in plan.excluded)
    assert all(entry.projected_balance_usdc is not None for entry in plan.ordered)
    for entry in plan.ordered:
        assert Decimal(entry.projected_balance_usdc) >= Decimal(plan.reserve_floor_usdc)


def test_expiring_discounts_are_ordered_first_then_lateness(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    plain = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-PLAIN")
    discounted = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=10, number="PLAN-DISC", discount_percent="2.00", discount_in_days=1)
    overdue = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=-5, number="PLAN-LATE")

    plan = PaymentPrioritiser(workflow).plan()
    order = [entry.invoice_id for entry in plan.ordered]

    assert order[0] == discounted.id, "an expiring discount must be ranked first"
    assert order.index(overdue.id) < order.index(plain.id), "lateness beats a distant due date"
    assert "early_payment_discount_expiring" in plan.ordered[0].reasons


def test_an_invoice_the_policy_blocks_is_excluded_with_its_reason(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=5_000 * USDC_SCALE, reserve=Decimal("0"))
    good = _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="PLAN-OK")
    # The seeded suspicious invoice carries an untrusted payee and a source mismatch.
    plan = PaymentPrioritiser(workflow).plan()
    ordered_ids = [entry.invoice_id for entry in plan.ordered]
    assert good.id in ordered_ids
    assert all(EXCLUDED_NOT_ELIGIBLE in entry.reasons for entry in plan.excluded if entry.invoice_id != good.id)
    blocked = next(entry for entry in plan.excluded if entry.invoice_id not in ordered_ids)
    assert blocked.reason and blocked.action == DecisionAction.HOLD.value


def test_building_a_plan_does_not_mutate_workflow_state(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    invoice = _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="PLAN-READONLY")
    before_state = store.get_state(invoice.id)
    before_events = len(workflow.events(invoice.id))

    PaymentPrioritiser(workflow).plan()

    assert store.get_state(invoice.id) == before_state
    assert len(workflow.events(invoice.id)) == before_events
    assert store.get_payment(invoice.id) is None


def test_the_plan_is_stable_and_digestible(tmp_path):
    _, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=50 * USDC_SCALE, due_in_days=0, number="PLAN-STABLE")
    first = PaymentPrioritiser(workflow).plan()
    second = PaymentPrioritiser(workflow).plan()
    assert plan_digest(first) == plan_digest(second)


# --------------------------------------------------------------------------------------
# The advisory part: a planner may reorder, never reallocate
# --------------------------------------------------------------------------------------


def _reversing_planner(settings, calls: list):
    """A planner that returns the reverse of exactly what it was offered."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        offered = json.loads(body["messages"][1]["content"])["invoices"]
        order = [
            {"invoice_id": item["invoice_id"], "reason": f"Placed at position {index + 1} by the planner."}
            for index, item in enumerate(reversed(offered))
        ]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps({"order": order})}}]})

    return DeliberatingPlanner(_planner_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))


def _fixed_order_planner(settings, order: list[str]):
    """A scripted planner that returns exactly the order it was given, valid or not."""

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"order": [
            {"invoice_id": invoice_id, "reason": f"Position {index + 1}."}
            for index, invoice_id in enumerate(order)
        ]}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(payload)}}]})

    return DeliberatingPlanner(_planner_settings(), client=httpx.Client(transport=httpx.MockTransport(handler)))


def _queue_scenario(tmp_path):
    """300 USDC spendable and four competing payables, the shape of the tight-treasury case."""
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=300 * USDC_SCALE, reserve=Decimal("0"))
    discount = _add_invoice(workflow, store, amount=150 * USDC_SCALE, due_in_days=20, number="PLAN-DISCOUNT", discount_percent="2.00", discount_in_days=1)
    overdue = _add_invoice(workflow, store, amount=150 * USDC_SCALE, due_in_days=-5, number="PLAN-OVERDUE")
    today = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-TODAY")
    legitimate = store.get_invoice("demo-invoice-legitimate")
    return settings, store, workflow, legitimate, discount, overdue, today


def test_a_planner_reordering_changes_which_invoices_are_selected(tmp_path):
    """The model's order decides which invoices fit, not only their sequence."""
    settings, _, workflow, legitimate, discount, overdue, today = _queue_scenario(tmp_path)
    offered_ids = {item["invoice"].id for item in PaymentPrioritiser(workflow).candidates() if item["eligible"]}
    assert len(offered_ids) == 4, "precondition: four individually payable invoices compete for 300 USDC"

    baseline = PaymentPrioritiser(workflow).plan()
    assert {entry.invoice_number for entry in baseline.ordered} == {"PLAN-DISCOUNT", "PLAN-OVERDUE"}
    planner = _fixed_order_planner(settings, [overdue.id, today.id, discount.id, legitimate.id])
    plan = PaymentPrioritiser(workflow, planner=planner).plan()

    baseline_ids = {entry.invoice_id for entry in baseline.ordered}
    model_ids = [entry.invoice_id for entry in plan.ordered]
    assert set(model_ids) == {overdue.id, today.id}, "the model order must change which invoices are paid"
    assert set(model_ids) != baseline_ids
    assert len(model_ids) == len(set(model_ids)), "no invoice may be selected twice"
    assert set(model_ids) <= offered_ids, "the model may not add an invoice that was not offered"
    assert Decimal(plan.planned_spend_usdc) == Decimal("250.00")
    assert Decimal(plan.planned_spend_usdc) <= Decimal(plan.spendable_usdc)
    assert Decimal(plan.balance_usdc) - Decimal(plan.planned_spend_usdc) >= Decimal(plan.reserve_floor_usdc)


def test_a_same_selection_in_a_different_sequence_is_not_a_selection_change(tmp_path):
    """Two invoices both fit: reversing them changes the sequence, not the paid set."""
    settings, _, workflow, legitimate, discount, overdue, today = _queue_scenario(tmp_path)
    baseline = PaymentPrioritiser(workflow).plan()
    planner = _fixed_order_planner(settings, [overdue.id, discount.id, today.id, legitimate.id])
    plan = PaymentPrioritiser(workflow, planner=planner).plan()

    baseline_ids = [entry.invoice_id for entry in baseline.ordered]
    model_ids = [entry.invoice_id for entry in plan.ordered]
    assert model_ids == [overdue.id, discount.id]
    assert model_ids != baseline_ids, "the recorded order reverses the sequence"
    assert set(model_ids) == set(baseline_ids), "the selected set did not change"

    module = _demo_module()
    assert module._selection_changed(["PLAN-DISCOUNT", "PLAN-OVERDUE"], ["PLAN-OVERDUE", "PLAN-DISCOUNT"]) is False
    assert module._selection_changed(["PLAN-DISCOUNT", "PLAN-OVERDUE"], ["PLAN-OVERDUE", "PLAN-TODAY"]) is True
    verdict = module._ordering_verdict(
        {
            "selection_changed": module._selection_changed(
                [entry.invoice_number for entry in baseline.ordered],
                [entry.invoice_number for entry in plan.ordered],
            ),
            "model_order_valid": plan.ordered_by == "planner",
            "deliberations": list(plan.deliberations),
        }
    )
    assert verdict["result"] == "INCONCLUSIVE"
    assert verdict["ordering_status"] == "same"


def test_an_invalid_or_padded_order_falls_back_to_the_rule_order(tmp_path):
    settings, _, workflow, legitimate, discount, overdue, today = _queue_scenario(tmp_path)
    baseline_ids = [entry.invoice_id for entry in PaymentPrioritiser(workflow).plan().ordered]
    for order, expected_outcome in (
        ([legitimate.id, discount.id, overdue.id, today.id, "invoice-not-offered"], "rejected:unknown_or_missing_invoice"),
        ([discount.id, discount.id, overdue.id, today.id], "rejected:duplicate_invoice"),
    ):
        plan = PaymentPrioritiser(workflow, planner=_fixed_order_planner(settings, order)).plan()
        assert plan.ordered_by == "heuristics", "an invalid order leaves the rule-based order in place"
        assert [entry.invoice_id for entry in plan.ordered] == baseline_ids
        assert plan.deliberations[0]["outcome"] == expected_outcome


def test_the_demo_ordering_block_names_both_selections_and_the_spend():
    """The recording output must show both selected sets, the computed spend and what is left unpaid."""
    module = _demo_module()
    case = {
        "ordering_status": "changed",
        "spendable_usdc": "300",
        "balance_usdc": "300",
        "reserve_floor_usdc": "0",
        "rule_based_spend_usdc": "300",
        "model_spend_usdc": "250",
        "rule_based_selected": ["PLAN-DISCOUNT", "PLAN-OVERDUE"],
        "model_selected": ["PLAN-OVERDUE", "PLAN-TODAY"],
        "sequence_changed": True,
        "rows": [
            {"invoice": "PLAN-OVERDUE", "amount_usdc": "150", "rule": "paid, 2nd", "model": "paid, 1st"},
            {"invoice": "PLAN-TODAY", "amount_usdc": "100", "rule": "not paid", "model": "paid, 2nd"},
            {"invoice": "PLAN-DISCOUNT", "amount_usdc": "150", "rule": "paid, 1st", "model": "not paid, over budget"},
            {"invoice": "ACME-INV-2026-001", "amount_usdc": "250", "rule": "not paid", "model": "not paid, over budget"},
        ],
        "model_left_unpaid": [
            {"invoice": "PLAN-DISCOUNT", "overdue_days": None, "discount_deadline": "2026-10-16"},
            {"invoice": "ACME-INV-2026-001", "overdue_days": None, "discount_deadline": None},
        ],
    }
    text = "\n".join(module._ordering_lines(case))
    assert text.splitlines()[0] == "PASS  model order changed which invoices are paid, spend 250.00 of 300.00 USDC"
    assert "Spend, computed:             300.00             250.00" in text
    assert "Sequence changed:   yes, same invoices in a different order" in text
    assert "Model left unpaid:  PLAN-DISCOUNT (discount ends 2026-10-16), ACME-INV-2026-001" in text
    assert all(len(line) <= 80 for line in text.splitlines())
    assert not any(word in text.lower() for word in ("better", "worse", "optimal"))


def test_a_valid_order_that_keeps_the_selection_is_inconclusive_not_a_pass():
    module = _demo_module()
    case = {
        "ordering_status": "same",
        "spendable_usdc": "400",
        "balance_usdc": "400",
        "reserve_floor_usdc": "0",
        "rule_based_selected": ["PLAN-DISCOUNT", "PLAN-OVERDUE", "PLAN-TODAY"],
        "model_selected": ["PLAN-OVERDUE", "PLAN-TODAY", "PLAN-DISCOUNT"],
    }
    text = "\n".join(module._ordering_lines(case))
    assert text.splitlines()[0] == (
        "INCONCLUSIVE  valid order, selection unchanged; demo did not show a selection change"
    )
    assert "FAIL" not in text
    assert "Model order:        PLAN-OVERDUE, PLAN-TODAY, PLAN-DISCOUNT" in text


def test_a_planner_may_reorder_the_queue(tmp_path):
    calls: list = []
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-LATER")
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-URGENT")
    prioritiser = PaymentPrioritiser(workflow)
    deterministic = [item["invoice"].id for item in prioritiser.candidates() if item["eligible"]]
    assert len(deterministic) >= 2

    plan = PaymentPrioritiser(workflow, planner=_reversing_planner(settings, calls)).plan()

    assert len(calls) == 1
    assert plan.ordered_by == "planner"
    assert plan.ordered[0].reason.startswith("Placed at position 1")
    # The queue is reversed relative to the fast layer, but every invoice is still there.
    assert [entry.invoice_id for entry in plan.ordered] == list(reversed(deterministic))[: len(plan.ordered)]


def test_a_planner_cannot_add_an_invoice_it_was_not_given(tmp_path):
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    first = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-1")
    second = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-2")
    planner = _planner(settings, {"order": [
        {"invoice_id": "invoice-that-was-never-offered", "reason": "pay me"},
        {"invoice_id": first.id, "reason": "ok"},
        {"invoice_id": second.id, "reason": "ok"},
    ]})

    plan = PaymentPrioritiser(workflow, planner=planner).plan()

    assert plan.ordered_by == "heuristics"
    ordered_ids = {entry.invoice_id for entry in plan.ordered}
    assert {first.id, second.id} <= ordered_ids
    assert "invoice-that-was-never-offered" not in ordered_ids
    assert plan.deliberations[0]["outcome"] == "rejected:unknown_or_missing_invoice"


def test_a_planner_cannot_omit_or_duplicate_invoices(tmp_path):
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    first = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-3")
    second = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-4")
    for reply, expected in (
        ({"order": [{"invoice_id": first.id, "reason": "only one"}]}, "rejected:unknown_or_missing_invoice"),
        ({"order": [
            {"invoice_id": first.id, "reason": "a"},
            {"invoice_id": first.id, "reason": "b"},
            {"invoice_id": second.id, "reason": "c"},
        ]}, "rejected:duplicate_invoice"),
        ({"order": [{"invoice_id": first.id, "reason": ""}, {"invoice_id": second.id, "reason": "b"}]}, "rejected:missing_reason"),
    ):
        plan = PaymentPrioritiser(workflow, planner=_planner(settings, reply)).plan()
        assert plan.deliberations[0]["outcome"] == expected
        assert plan.ordered_by == "heuristics"


def test_an_unavailable_planner_leaves_the_deterministic_order(tmp_path):
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=1_000 * USDC_SCALE, reserve=Decimal("0"))
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-5")
    _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-6")
    plan = PaymentPrioritiser(workflow, planner=_planner(settings, None, status_code=500)).plan()
    assert plan.ordered_by == "heuristics"
    assert plan.deliberations[0]["outcome"] == "unavailable"


def test_a_planner_reordering_cannot_increase_the_spend(tmp_path):
    """The strongest guarantee: order is advisory, allocation is code."""
    settings, store, workflow, _ = _workspace(tmp_path, balance_units=150 * USDC_SCALE, reserve=Decimal("0"))
    small = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-SMALL")
    large = _add_invoice(workflow, store, amount=100 * USDC_SCALE, due_in_days=0, number="PLAN-LARGE")
    baseline = PaymentPrioritiser(workflow).plan()

    # A planner asks to put both first; only the first is affordable, so only one is payable.
    planner = _planner(settings, {"order": [
        {"invoice_id": large.id, "reason": "first"},
        {"invoice_id": small.id, "reason": "second"},
    ]})
    plan = PaymentPrioritiser(workflow, planner=planner).plan()

    assert Decimal(plan.planned_spend_usdc) <= Decimal(plan.spendable_usdc)
    assert len(plan.ordered) == 1
    assert Decimal(baseline.planned_spend_usdc) == Decimal(plan.planned_spend_usdc)
    assert any(EXCLUDED_BELOW_RESERVE in entry.reasons for entry in plan.excluded)


def test_order_planner_is_only_built_when_deliberation_is_configured(tmp_path):
    assert build_order_planner(Settings(_env_file=None, decision_layer="heuristics")) is None
    assert build_order_planner(Settings(_env_file=None, decision_layer="dual_process")) is None  # no endpoint
    configured = Settings(_env_file=None, decision_layer="dual_process", planner_base_url="https://planner.test/v1", planner_model="m")
    assert build_order_planner(configured) is not None


# --------------------------------------------------------------------------------------
# The endpoint
# --------------------------------------------------------------------------------------


def test_the_plan_endpoint_requires_a_key_and_reports_the_plan(tmp_path):
    from fastapi.testclient import TestClient

    from arc_payables.api import create_app

    settings = Settings(_env_file=None, database_path=tmp_path / "api-plan.sqlite3", api_key="k", min_reserve_usdc=Decimal("0"))
    app = create_app(settings=settings)
    client = TestClient(app)
    assert client.get("/plan").status_code == 401
    body = client.get("/plan", headers={"X-API-Key": "k"}).json()
    assert {"ordered", "excluded", "balance_usdc", "reserve_floor_usdc", "spendable_usdc", "planned_spend_usdc", "rationale"} <= set(body)
    assert Decimal(body["planned_spend_usdc"]) <= Decimal(body["spendable_usdc"])
