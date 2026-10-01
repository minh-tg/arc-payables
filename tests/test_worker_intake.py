"""Discovery: the worker brings in what the ledger owes, and still decides nothing by itself.

The worker's other steps repair work a decision already authorized. This one is different, because
it changes what is in the queue. So the tests here are about the boundary: the pass may find a
payable and evaluate it, and it may not approve one, skip a policy check, or pay something the
policy refused.
"""

from __future__ import annotations

from decimal import Decimal

from arc_payables import worker
from arc_payables.domain import WorkflowState

#: The seeded payable that exists in the simulated ERP and has never been captured locally.
IMPORTABLE = "PINV-ACME-2026-003"


def _step(report, name):
    return next(step for step in report.steps if step.name == name)


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
