"""Money-facing planning checks: asymmetric choices, stale inputs, and durable boundaries.

The planner transports below are local test doubles, not evidence of a real model's quality.
"""
from dataclasses import replace
from decimal import Decimal

import pytest

from arc_payables import worker
from arc_payables.domain import DecisionAction, USDC_SCALE, WorkflowState
from arc_payables.prioritisation import PaymentPrioritiser
from arc_payables.seed import APPROVED_WALLET
from arc_payables.store import SQLiteEvidenceStore
from test_prioritisation import _add_invoice, _reversing_planner, _workspace


def _queue(tmp_path, *, headroom=100):
    settings, store, workflow, provider = _workspace(
        tmp_path, balance_units=(2000 + headroom) * USDC_SCALE, reserve=Decimal("2000")
    )
    discount = _add_invoice(workflow, store, amount=60 * USDC_SCALE, due_in_days=5,
                            number="DISCOUNT", discount_percent="2", discount_in_days=1)
    overdue = _add_invoice(workflow, store, amount=60 * USDC_SCALE, due_in_days=-4, number="OVERDUE")
    for invoice in (discount, overdue):
        assert workflow.evaluate(invoice.id)["state"] == WorkflowState.ELIGIBLE.value
    return settings, store, workflow, provider, discount, overdue


def _autopay(workflow, **kwargs):
    report = worker.run_pass(workflow, autopay=True, rescreen=False, **kwargs)
    return next(step for step in report.steps if step.name == "autopay")


@pytest.mark.parametrize("model", [False, True])
def test_recorded_order_not_database_order_selects_the_affordable_obligation(tmp_path, monkeypatch, model):
    settings, store, workflow, provider, discount, overdue = _queue(tmp_path)
    calls = []
    if model:
        monkeypatch.setattr("arc_payables.deliberation.build_order_planner",
                            lambda _settings: _reversing_planner(settings, calls))
    submissions = []
    original = provider.submit_authorized

    def submit(payment, on_transaction=None):
        history = store.payment_plan_history()
        assert history[0]["status"] == "recorded", "advice must be durable before any broadcast"
        assert history[0]["plan"]["ordered"][0]["invoice_id"] == payment["invoice_id"]
        submissions.append(payment["invoice_id"])
        return original(payment, on_transaction)

    monkeypatch.setattr(provider, "submit_authorized", submit)
    step = _autopay(workflow)
    chosen, deferred = (overdue, discount) if model else (discount, overdue)
    assert submissions == [chosen.id]
    assert store.get_payment(deferred.id) is None
    assert provider.get_balance().balance_units == 2040 * USDC_SCALE
    assert step.acted == 1 and step.failed == 0
    executed = next(row for row in store.payment_plan_history() if row["status"] == "executed")
    assert executed["plan"]["ordered_by"] == ("planner" if model else "heuristics")
    assert executed["outcome"]["invoice_id"] == chosen.id
    assert len(calls) == int(model)
    events = store.events(chosen.id)
    types = [event["type"] for event in events]
    assert types.index("PAYMENT_PLAN_RECORDED") < types.index("PAYMENT_AUTHORIZED")
    assert store.verify_audit_chain()["ok"]


@pytest.mark.parametrize("model", [False, True])
def test_competing_critical_supplier_discount_and_less_urgent_obligation(tmp_path, monkeypatch, model):
    settings, store, workflow, provider, discount, overdue = _queue(tmp_path)
    # A separately trusted supplier, order and receipt; criticality is an operator setting,
    # not a persuasive note on the invoice or an invented model observation.
    critical_id = "SUP-CRITICAL"
    original_supplier = store.get_fixture("supplier", overdue.supplier_id)
    store.seed_fixture("supplier", critical_id, {**original_supplier, "id": critical_id, "erp_supplier_id": critical_id})
    new_po, new_receipt = "PO-CRITICAL", "PR-CRITICAL"
    for kind, old_id, new_id in (("purchase_order", overdue.purchase_order_ids[0], new_po),
                                 ("receipt", overdue.receipt_ids[0], new_receipt)):
        fixture = store.get_fixture(kind, old_id)
        store.seed_fixture(kind, new_id, {**fixture, "id": new_id, "supplier_id": critical_id})
    critical = replace(overdue, supplier_id=critical_id, purchase_order_ids=(new_po,), receipt_ids=(new_receipt,))
    store.update_invoice_record(critical, "TEST_TRUSTED_SUPPLIER")
    payable = store.get_fixture("erp_payable", overdue.purchase_invoice_id)
    store.seed_fixture("erp_payable", overdue.purchase_invoice_id, {
        **payable, "supplier_id": critical_id, "purchase_order_ids": [new_po], "receipt_ids": [new_receipt]})
    settings.critical_supplier_ids = (critical_id,)
    assert workflow.evaluate(critical.id)["state"] == WorkflowState.ELIGIBLE.value
    less_urgent = _add_invoice(workflow, store, amount=60 * USDC_SCALE, due_in_days=2, number="LESS-URGENT")
    assert workflow.evaluate(less_urgent.id)["state"] == WorkflowState.ELIGIBLE.value
    offered = []

    class DiscountPlanner:
        def order_payables(self, summary, balance, reserve):
            offered.extend(summary)
            ids = [discount.id, critical.id, less_urgent.id]
            return [(invoice_id, "Prefer expiring savings; critical obligation remains visible for escalation.")
                    for invoice_id in ids], {"outcome": "used", "model": "local-test-double"}

    if model:
        monkeypatch.setattr("arc_payables.deliberation.build_order_planner", lambda _settings: DiscountPlanner())
    step = _autopay(workflow)
    chosen = discount if model else critical
    assert step.acted == 1
    assert store.get_payment(chosen.id) is not None
    assert store.get_payment(less_urgent.id) is None
    assert provider.get_balance().balance_units == 2040 * USDC_SCALE
    executed = next(row for row in store.payment_plan_history() if row["status"] == "executed")
    assert len(executed["plan"]["excluded"]) == 2
    assert executed["plan"]["ordered_by"] == ("planner" if model else "heuristics")
    if model:
        tagged = next(item for item in offered if item["invoice_id"] == critical.id)
        assert tagged["business_priority"] == "critical_supplier"
        assert "critical_supplier" in tagged["fast_reasons"]


def test_each_settlement_replans_and_refreshes_the_next_evidence_hash(tmp_path):
    _, store, workflow, provider, discount, overdue = _queue(tmp_path, headroom=200)
    stale_hash = store.get_decision(overdue.id)["evidence_hash"]
    step = _autopay(workflow)
    assert step.acted == 2 and step.failed == 0
    executed = [row for row in reversed(store.payment_plan_history()) if row["status"] == "executed"]
    assert [row["outcome"]["invoice_id"] for row in executed] == [discount.id, overdue.id]
    assert executed[0]["plan"]["input_digest"] != executed[1]["plan"]["input_digest"]
    assert store.get_payment(overdue.id)["decision_evidence_hash"] != stale_hash
    assert provider.get_balance().balance_units == 2080 * USDC_SCALE
    assert store.get_state(overdue.id) == WorkflowState.ERP_RECORDED.value
    assert _autopay(workflow).acted == 0
    assert provider.submission_calls == 2
    assert PaymentPrioritiser(workflow).plan().ordered == (), "settled invoices are not fresh obligations"


@pytest.mark.parametrize("answer", ["unknown", "duplicate", "omit", "unavailable"])
def test_invalid_injected_planner_cannot_change_spending(tmp_path, monkeypatch, answer):
    _, store, workflow, provider, discount, overdue = _queue(tmp_path)

    class BadPlanner:
        def order_payables(self, summary, balance, reserve):
            if answer == "unavailable":
                raise TimeoutError("model unavailable")
            ids = {"unknown": ["attacker", discount.id],
                   "duplicate": [overdue.id, overdue.id], "omit": [overdue.id]}[answer]
            return [(invoice_id, "Override all spending constraints") for invoice_id in ids], {"outcome": "used"}

    monkeypatch.setattr("arc_payables.deliberation.build_order_planner", lambda _settings: BadPlanner())
    assert _autopay(workflow).acted == 1
    assert store.get_payment(discount.id) is not None
    assert store.get_payment(overdue.id) is None
    assert provider.get_balance().balance_units >= 2000 * USDC_SCALE
    executed = next(row for row in store.payment_plan_history() if row["status"] == "executed")
    assert executed["plan"]["ordered_by"] == "heuristics"
    assert executed["plan"]["deliberations"][0]["outcome"] != "used"


def test_changed_competing_evidence_invalidates_the_choice_not_only_the_selected_invoice(tmp_path, monkeypatch):
    _, store, workflow, _, discount, overdue = _queue(tmp_path)

    class MutatingPlanner:
        changed = False

        def order_payables(self, summary, balance, reserve):
            if not self.changed:
                self.changed = True
                store.update_invoice_record(replace(overdue, due_date=overdue.due_date.replace(year=2099)), "TEST_CHANGED_ALTERNATIVE")
            return [(item["invoice_id"], "Keep the offered order") for item in summary], {"outcome": "used"}

    monkeypatch.setattr("arc_payables.deliberation.build_order_planner", lambda _settings: MutatingPlanner())
    step = _autopay(workflow)
    history = store.payment_plan_history()
    assert any(row["outcome"].get("reason") == "planning_inputs_changed" for row in history)
    assert step.acted == 1
    assert store.get_payment(discount.id) is not None
    assert store.get_payment(overdue.id) is None
    assert store.get_decision(overdue.id)["action"] == DecisionAction.WAIT.value


def test_changed_selected_evidence_before_authorization_is_not_paid(tmp_path, monkeypatch):
    _, store, workflow, provider, discount, _ = _queue(tmp_path)
    original = workflow.evaluate
    changed = False

    def evaluate(invoice_id):
        nonlocal changed
        if not changed:
            changed = True
            supplier = store.get_fixture("supplier", discount.supplier_id)
            store.seed_fixture("supplier", discount.supplier_id, {**supplier, "wallet_verified": False})
        return original(invoice_id)

    monkeypatch.setattr(workflow, "evaluate", evaluate)
    step = _autopay(workflow)
    assert step.acted == 0
    assert provider.submission_calls == 0
    assert any(row["status"] == "invalidated" for row in store.payment_plan_history())
    assert store.get_decision(discount.id)["action"] != DecisionAction.PAY_NOW.value


def test_changed_approval_evidence_is_not_reapproved_by_worker(tmp_path):
    _, store, workflow, provider, discount, overdue = _queue(tmp_path)
    workflow.settings.max_invoice_usdc = Decimal("10")
    workflow.evaluate(discount.id)
    decision = store.get_decision(discount.id)
    checks = [check["code"] for check in decision["policy_checks"]
              if check.get("requires_human") and check.get("overridable") and not check["passed"]]
    workflow.approve(discount.id, {"reviewer": "test-human", "approved": True,
                                  "note": "Approved only this evidence", "acknowledged_checks": checks})
    assert store.get_state(discount.id) == WorkflowState.ELIGIBLE.value
    provider._balance_units += USDC_SCALE  # Makes the scoped human approval stale.
    step = _autopay(workflow)
    assert step.acted == 0 and provider.submission_calls == 0
    assert store.get_state(discount.id) == WorkflowState.ESCALATED.value
    assert store.get_payment(overdue.id) is None


def test_material_evidence_refusal_between_validation_and_submit_is_recorded(tmp_path, monkeypatch):
    _, store, workflow, provider, discount, _ = _queue(tmp_path)
    original = workflow.submit_payment

    def submit(invoice_id):
        store.seed_fixture("screening", APPROVED_WALLET.lower(), {"status": "FLAGGED"})
        return original(invoice_id)

    monkeypatch.setattr(workflow, "submit_payment", submit)
    step = _autopay(workflow)
    assert step.acted == 0 and provider.submission_calls == 0
    assert step.detail["refusals"] == {"evidence_changed_after_evaluation": 1}
    assert any(row["outcome"].get("reason") == "evidence_changed_after_evaluation"
               for row in store.payment_plan_history())
    assert store.get_payment(discount.id) is None


def test_uncertain_settlement_stops_new_spending_even_when_reported_balance_did_not_fall(tmp_path):
    _, store, workflow, provider, _, _ = _queue(tmp_path, headroom=200)
    provider.failure_mode = "rpc_timeout"
    step = _autopay(workflow)
    assert step.acted == 1
    assert provider.submission_calls == 1
    assert provider.get_balance().balance_units == 2200 * USDC_SCALE
    assert step.detail["stopped_reason"] == "pending_settlement"
    assert _autopay(workflow).acted == 0
    assert provider.submission_calls == 1


def test_one_unavailable_evidence_row_does_not_drop_an_independent_payable(tmp_path, monkeypatch):
    _, store, workflow, provider, discount, overdue = _queue(tmp_path)
    original = workflow.accounting.get_invoice_evidence

    def evidence(invoice):
        if invoice.id == overdue.id:
            raise RuntimeError("One source record is temporarily unavailable")
        return original(invoice)

    monkeypatch.setattr(workflow.accounting, "get_invoice_evidence", evidence)
    step = _autopay(workflow)
    assert step.acted == 1 and step.failed == 1
    assert step.detail["refusals"] == {"evidence_unavailable": 1}
    assert store.get_payment(discount.id) is not None
    assert store.get_payment(overdue.id) is None
    assert provider.submission_calls == 1
    invalidated = next(row for row in store.payment_plan_history() if row["status"] == "invalidated")
    assert invalidated["outcome"]["invoice_id"] == overdue.id


def test_plan_storage_failure_is_fail_closed(tmp_path, monkeypatch):
    _, store, workflow, provider, _, _ = _queue(tmp_path)

    def broken(*args):
        raise RuntimeError("plan database is unavailable")

    monkeypatch.setattr(store, "record_payment_plan", broken)
    step = _autopay(workflow)
    assert step.failed == 1
    assert provider.submission_calls == 0


def test_outcome_storage_failure_after_payment_stops_second_payment(tmp_path, monkeypatch):
    _, store, workflow, provider, _, _ = _queue(tmp_path, headroom=200)

    def broken(*args):
        raise RuntimeError("outcome database is unavailable")

    monkeypatch.setattr(store, "finish_payment_plan", broken)
    step = _autopay(workflow)
    assert step.failed == 1 and provider.submission_calls == 1
    assert store.payment_plan_history()[0]["status"] == "recorded"
    # A recorded plan has no completion proof; a later pass rebuilds, it never replays advice.
    assert "outcome database" in step.error


@pytest.mark.parametrize("budget", [0, 1, 2])
def test_payment_attempts_stay_within_budget(tmp_path, budget):
    _, _, workflow, provider, _, _ = _queue(tmp_path, headroom=200)
    step = _autopay(workflow, max_actions=budget)
    assert step.acted == budget
    assert provider.submission_calls == budget


def test_intake_and_payment_share_one_action_budget(runtime):
    report = worker.run_pass(runtime["workflow"], intake=True, autopay=True, rescreen=False, max_actions=1)
    assert next(step for step in report.steps if step.name == "intake").acted == 1
    assert next(step for step in report.steps if step.name == "autopay").acted == 0
    assert runtime["payment"].submission_calls == 0


@pytest.mark.parametrize("once", [False, True])
def test_cli_honors_an_explicit_zero_action_budget(runtime, monkeypatch, once):
    monkeypatch.setattr("arc_payables.runtime.build_workflow", lambda _settings: runtime["workflow"])
    monkeypatch.setattr("arc_payables.settings.get_settings", lambda: runtime["settings"])
    monkeypatch.setattr("sys.argv", ["arc-payables-worker", "--autopay", "--max-actions", "0"] + (["--once"] if once else []))
    if once:
        runtime["workflow"].evaluate(runtime["legitimate_id"])
        with pytest.raises(SystemExit) as exc:
            worker.main()
        assert exc.value.code == 0
        assert runtime["payment"].submission_calls == 0
    else:
        seen = []
        monkeypatch.setattr(worker, "run_forever", lambda workflow, **options: seen.append(options))
        worker.main()
        assert seen[0]["max_actions"] == 0


def test_continuously_changing_inputs_are_bounded_without_payment(tmp_path, monkeypatch):
    _, _, workflow, provider, _, _ = _queue(tmp_path)
    monkeypatch.setattr(PaymentPrioritiser, "current_input_digest", lambda self: "always-changed")
    step = _autopay(workflow, max_actions=1)
    assert len(step.detail["plans"]) == 4
    assert step.detail["stopped_reason"] == "replan_limit_reached"
    assert step.failed == 1 and provider.submission_calls == 0


def test_concurrent_authorizations_cannot_both_spend_the_same_headroom(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from arc_payables.service import WorkflowError

    _, store, workflow, provider, discount, overdue = _queue(tmp_path)
    barrier = Barrier(2)
    original = workflow.signer.sign

    def sign(permit):
        signature = original(permit)
        barrier.wait(timeout=10)  # Both have independently passed checks on the old balance.
        return signature

    monkeypatch.setattr(workflow.signer, "sign", sign)

    def submit(invoice_id):
        try:
            workflow.submit_payment(invoice_id)
            return "paid"
        except WorkflowError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, [discount.id, overdue.id]))
    assert sorted(outcomes) == ["paid", "payment_authorization_conflict"]
    assert provider.submission_calls == 1
    assert provider.get_balance().balance_units == 2040 * USDC_SCALE
    assert store.payment_authorization_revision() == 1


def test_human_hold_during_signing_cannot_be_overwritten_by_authorization(tmp_path, monkeypatch):
    from arc_payables.service import WorkflowError

    _, store, workflow, provider, discount, _ = _queue(tmp_path)
    original = workflow.signer.sign

    def sign(permit):
        store.set_state(discount.id, WorkflowState.HELD.value, "TEST_HUMAN_HOLD")
        return original(permit)

    monkeypatch.setattr(workflow.signer, "sign", sign)
    with pytest.raises(WorkflowError) as exc:
        workflow.submit_payment(discount.id)
    assert exc.value.code == "payment_authorization_conflict"
    assert provider.submission_calls == 0
    assert store.get_state(discount.id) == WorkflowState.HELD.value


@pytest.mark.parametrize("change", ["invoice", "approval"])
def test_invoice_or_approval_changed_during_signing_cannot_use_stale_authority(tmp_path, monkeypatch, change):
    from arc_payables.service import WorkflowError

    _, store, workflow, provider, discount, _ = _queue(tmp_path)
    original = workflow.signer.sign

    def sign(permit):
        if change == "invoice":
            store.update_invoice_record(replace(discount, discount_percent="99"), "TEST_CHANGED_DURING_SIGNING")
        else:
            store.record_approval(discount.id, {"approved": False, "reviewer": "test-human",
                                               "note": "Stop", "acknowledged_checks": []})
        return original(permit)

    monkeypatch.setattr(workflow.signer, "sign", sign)
    with pytest.raises(WorkflowError) as exc:
        workflow.submit_payment(discount.id)
    assert exc.value.code == "payment_authorization_conflict"
    assert provider.submission_calls == 0
    assert store.get_payment(discount.id) is None


def test_a_manual_payment_also_cannot_ignore_pending_treasury_authorizations(tmp_path):
    from arc_payables.service import WorkflowError

    _, store, workflow, provider, discount, overdue = _queue(tmp_path, headroom=200)
    provider.failure_mode = "rpc_timeout"
    workflow.submit_payment(discount.id)
    workflow.evaluate(overdue.id)
    with pytest.raises(WorkflowError) as exc:
        workflow.submit_payment(overdue.id)
    assert exc.value.code == "payment_authorization_conflict"
    assert provider.submission_calls == 1
    assert store.get_payment(overdue.id) is None


def test_plan_records_survive_restart_and_never_include_settings_secrets(tmp_path):
    settings, store, workflow, _, _, _ = _queue(tmp_path)
    settings.planner_api_key = "test-secret-must-not-be-stored"
    _autopay(workflow)
    restarted = SQLiteEvidenceStore(settings.database_path)
    restarted.initialize()
    assert restarted.payment_plan_history() == store.payment_plan_history()
    assert settings.planner_api_key not in str(restarted.payment_plan_history())
    with pytest.raises(ValueError, match="open recorded"):
        row = restarted.payment_plan_history()[0]
        restarted.finish_payment_plan(row["id"], "executed", {})
