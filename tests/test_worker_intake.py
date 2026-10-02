"""Discovery: the worker brings in what the ledger owes, and still decides nothing by itself.

The worker's other steps repair work a decision already authorized. This one is different, because
it changes what is in the queue. So the tests here are about the boundary: the pass may find a
payable and evaluate it, and it may not approve one, skip a policy check, or pay something the
policy refused.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

from arc_payables import worker
from arc_payables.domain import WorkflowState

#: The seeded payable that exists in the simulated ERP and has never been captured locally.
IMPORTABLE = "PINV-ACME-2026-003"


def _step(report, name):
    return next(step for step in report.steps if step.name == name)


def _set_due_date(runtime, invoice_id: str, due) -> None:
    """Move a payable's due date, which is what time passing means to the policy."""
    invoice = runtime["store"].get_invoice(invoice_id)
    runtime["store"].update_invoice_record(replace(invoice, due_date=due), "TEST_DUE_DATE")


def _park_then_come_due(runtime, invoice_id: str, *, due_in_days: int = 1) -> None:
    """Put a payable in the state time creates: decided as waiting, and now due.

    The due date moves without a second evaluation, which is exactly the situation between passes.
    The decision is from before, and only the calendar has changed since.
    """
    today = date.today()
    _set_due_date(runtime, invoice_id, today + timedelta(days=30))
    assert runtime["workflow"].evaluate(invoice_id)["state"] == WorkflowState.WAITING.value
    _set_due_date(runtime, invoice_id, today + timedelta(days=due_in_days))


def test_a_payable_waiting_for_its_due_date_becomes_payable_without_a_person(runtime):
    """The case that otherwise waits for ever.

    A payable normally arrives before it is due, gets decided as waiting, and then nothing looked at
    it again: discovery only ever examines payables it has never seen. So the day it became payable,
    the only way forward was a person pressing evaluate.
    """
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)  # capture everything first
    _park_then_come_due(runtime, runtime["legitimate_id"])

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.acted == 1
    assert intake.detail["reconsidered"][0]["was"] == WorkflowState.WAITING.value
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ELIGIBLE.value
    # Evaluating is not authority to pay: that stays the separate autopay decision.
    assert runtime["store"].get_payment(runtime["legitimate_id"]) is None


def test_a_payable_that_is_not_due_yet_is_left_waiting(runtime):
    """Waiting is a decision. The worker must not talk itself out of it."""
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)
    today = date.today()
    invoice_id = runtime["legitimate_id"]
    _set_due_date(runtime, invoice_id, today + timedelta(days=30))
    assert runtime["workflow"].evaluate(invoice_id)["state"] == WorkflowState.WAITING.value

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.acted == 0
    assert "reconsidered" not in intake.detail
    assert runtime["store"].get_state(invoice_id) == WorkflowState.WAITING.value
    assert runtime["store"].get_payment(invoice_id) is None


def test_coming_due_is_deferred_not_skipped_when_the_budget_is_spent(runtime):
    """A pass that ran out of actions has to say so, rather than leaving it for the next one silently."""
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)
    _park_then_come_due(runtime, runtime["legitimate_id"])

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False, max_actions=0)

    intake = _step(report, "intake")
    assert intake.acted == 0
    assert intake.detail["deferred"] >= 1
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.WAITING.value


def test_a_held_payable_is_never_reconsidered(runtime):
    """A person parked it. The worker does not get to disagree, however the dates look."""
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)
    today = date.today()
    invoice_id = runtime["legitimate_id"]
    _set_due_date(runtime, invoice_id, today)
    runtime["store"].set_state(invoice_id, WorkflowState.HELD.value, "TEST_HELD")
    events = len(runtime["store"].events(invoice_id))

    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    assert runtime["store"].get_state(invoice_id) == WorkflowState.HELD.value
    assert len(runtime["store"].events(invoice_id)) == events, "a held invoice was written to"


def test_the_whole_chain_runs_from_arrival_to_paid_once_due(runtime):
    """Find it early, wait, then pay it and book it when the date arrives, with nobody watching."""
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)
    _park_then_come_due(runtime, runtime["legitimate_id"])

    report = worker.run_pass(runtime["workflow"], intake=True, autopay=True, rescreen=False)

    assert _step(report, "intake").acted == 1
    assert _step(report, "autopay").acted == 1
    invoice_id = runtime["legitimate_id"]
    assert runtime["store"].get_state(invoice_id) == WorkflowState.ERP_RECORDED.value
    assert runtime["store"].get_payment(invoice_id)["confirmation_status"] == "CONFIRMED"


def test_a_payable_imported_this_pass_is_not_decided_twice(runtime):
    """Reconsideration reads the queue as it stood before discovery, so new work is not redone."""
    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.acted == 1, "only the newly discovered payable was acted on"
    assert "reconsidered" not in intake.detail


def _external_ids(store) -> set[str]:
    return {invoice.purchase_invoice_id for invoice, _state in store.list_invoices()}


def _by_external_id(store, external_id: str):
    return next(invoice for invoice, _state in store.list_invoices() if invoice.purchase_invoice_id == external_id)


def test_a_pass_finds_and_evaluates_a_payable_nobody_imported(runtime):
    assert IMPORTABLE not in _external_ids(runtime["store"])

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.acted == 1
    assert IMPORTABLE in _external_ids(runtime["store"])
    # The two seeded payables are already captured, so discovery skips them rather than re-importing.
    assert intake.skipped == 2
    assert report.outcome == "ok"


def test_discovery_alone_does_not_move_money(runtime):
    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    assert _step(report, "intake").acted == 1
    invoice = _by_external_id(runtime["store"], IMPORTABLE)
    assert runtime["store"].get_state(invoice.id) == WorkflowState.ELIGIBLE.value
    assert runtime["store"].get_payment(invoice.id) is None


def test_discovery_and_autopay_settle_a_payable_with_nobody_in_the_loop(runtime):
    """The whole point: find it, decide it, pay it, book it, without a person or a second command."""
    report = worker.run_pass(runtime["workflow"], intake=True, autopay=True, rescreen=False)

    assert [step.name for step in report.steps] == ["reconcile", "writeback", "intake", "autopay", "observe"]
    assert _step(report, "intake").acted == 1
    assert _step(report, "autopay").acted == 1
    invoice = _by_external_id(runtime["store"], IMPORTABLE)
    assert runtime["store"].get_state(invoice.id) == WorkflowState.ERP_RECORDED.value
    assert runtime["store"].get_payment(invoice.id)["confirmation_status"] == "CONFIRMED"


def test_discovery_cannot_pay_what_the_policy_refused(runtime):
    """Finding a payable is not authority to pay it. An over-limit one escalates, untouched."""
    runtime["workflow"].settings.max_invoice_usdc = Decimal("1")

    report = worker.run_pass(runtime["workflow"], intake=True, autopay=True, rescreen=False)

    assert _step(report, "intake").acted == 1
    assert _step(report, "autopay").acted == 0
    invoice = _by_external_id(runtime["store"], IMPORTABLE)
    assert runtime["store"].get_state(invoice.id) == WorkflowState.ESCALATED.value
    assert runtime["store"].get_payment(invoice.id) is None


def test_a_repeated_pass_does_not_capture_the_same_payable_twice(runtime):
    worker.run_pass(runtime["workflow"], intake=True, rescreen=False)
    captured = len(runtime["store"].list_invoices())

    second = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    assert _step(second, "intake").acted == 0
    assert _step(second, "intake").skipped == 3
    assert len(runtime["store"].list_invoices()) == captured


def test_discovery_stays_inside_the_action_budget(runtime):
    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False, max_actions=0)

    intake = _step(report, "intake")
    assert intake.examined == 1
    assert intake.acted == 0
    assert intake.detail["deferred"] == 1
    assert IMPORTABLE not in _external_ids(runtime["store"])


def test_a_connector_that_cannot_enumerate_is_skipped_not_failed(runtime, monkeypatch):
    """A connector without discovery is a supported shape, not a fault to alert on."""
    monkeypatch.delattr(type(runtime["accounting"]), "list_open_payables", raising=False)

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.skipped == 1
    assert intake.failed == 0
    assert "cannot enumerate" in intake.detail["reason"]
    assert report.outcome == "ok"


def test_a_connector_that_fails_to_enumerate_degrades_the_pass(runtime, monkeypatch):
    def broken(self):
        raise RuntimeError("the ledger is unreachable")

    monkeypatch.setattr(type(runtime["accounting"]), "list_open_payables", broken)

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.failed == 1
    assert "the ledger is unreachable" in intake.error
    # One broken step is not a broken pass: the rest of the work still ran.
    assert report.outcome == "degraded"
    assert [step.name for step in report.steps] == ["reconcile", "writeback", "intake", "observe"]


def test_a_payable_the_system_will_not_settle_is_declined_with_its_reason(runtime):
    """A foreign-currency payable is a decision, not a fault: it must not degrade every pass."""
    from datetime import date

    from arc_payables.seed import _erp_payable

    base = runtime["store"].get_fixture("erp_payable", "PINV-ACME-2026-003")
    foreign = _erp_payable(
        external_id="PINV-ACME-2026-004",
        invoice_number="ACME-INV-2026-004",
        amount_units=base["amount_units"],
        invoice_currency="CAD",
        invoice_date=date.fromisoformat(base["invoice_date"]),
        due_date=date.fromisoformat(base["due_date"]),
        lines=(),
        purchase_order_ids=(),
        receipt_ids=(),
    )
    runtime["store"].seed_fixture("erp_payable", foreign["invoice_id"], foreign)

    report = worker.run_pass(runtime["workflow"], intake=True, rescreen=False)

    intake = _step(report, "intake")
    assert intake.acted == 1
    assert intake.skipped == 3  # two already captured, one declined
    assert intake.failed == 0
    assert report.outcome == "ok"
    declined = next(item for item in intake.detail["declined"] if item["external_id"] == "PINV-ACME-2026-004")
    assert declined["code"] == "accounting_read_failed"
    # The cause is carried too, or the operator cannot tell a missing rate from an outage.
    assert "CAD" in declined["reason"]


def test_the_intake_key_is_stable_and_per_payable():
    from arc_payables.worker import _intake_key

    assert _intake_key("PINV-1") == _intake_key("PINV-1")
    assert _intake_key("PINV-1") != _intake_key("PINV-2")


def test_the_loop_discovers_by_default_and_the_cli_can_stop_it():
    from arc_payables.settings import Settings
    from arc_payables.worker import _build_parser, _cli_or_setting

    settings = Settings(_env_file=None)
    assert settings.worker_intake is True
    assert _cli_or_setting(_build_parser().parse_args([]).intake, settings.worker_intake) is True
    assert _cli_or_setting(_build_parser().parse_args(["--no-intake"]).intake, settings.worker_intake) is False
    assert _cli_or_setting(_build_parser().parse_args(["--intake"]).intake, False) is True
