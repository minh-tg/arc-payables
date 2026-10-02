"""The background pass.

These are about failure behaviour, because that is the whole reason a worker exists. A pass must
finish when one row is broken, must refuse to spend without an explicit opt-in, must stay inside its
action budget, and must keep running after a bad pass.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from arc_payables.domain import utcnow

from arc_payables import worker
from arc_payables.domain import WorkflowState
from arc_payables.service import APWorkflow, WorkflowError


def _step(report, name):
    return next(step for step in report.steps if step.name == name)


def _set_payment(runtime, invoice_id: str, updates: dict, state: str) -> None:
    runtime["store"].update_payment(invoice_id, updates, state, "TEST_SETUP")


def _extra_eligible_invoice(runtime, external_id: str = "PINV-EXTRA-1") -> str:
    """A second payable the policy authorizes, so a pass can be shown to work through a queue.

    It reuses the demo's seeded purchase order and receipt, exactly as the demo's own importable
    invoice does, so the evidence checks are satisfied by real fixtures rather than by mocks of them.
    """
    from datetime import date, timedelta

    from arc_payables import seed
    from arc_payables.domain import InvoiceLine

    payable = seed._erp_payable(
        external_id=external_id,
        invoice_number=f"ACME-{external_id}",
        amount_units=250 * 10**6,
        invoice_currency="USD",
        invoice_date=date.today() - timedelta(days=20),
        # Due now: the policy deliberately waits on invoices that are not due soon.
        due_date=date.today(),
        lines=(InvoiceLine("INDUSTRIAL-FILTER", "10", 250 * 10**6, "POL-ACME-2026-001-1",
                           ("PRL-ACME-2026-001-1",)),),
        purchase_order_ids=("PO-ACME-2026-001",),
        receipt_ids=("PR-ACME-2026-001",),
        payment_terms="Net 7",
    )
    runtime["store"].seed_fixture("erp_payable", external_id, payable)
    invoice = seed._local_invoice(payable, Decimal("1"), payee_address=seed.APPROVED_WALLET)
    created, _ = runtime["workflow"].create_invoice(invoice, str(uuid.uuid4()))
    return created.id


def test_a_pass_records_what_it_did(runtime):
    report = worker.run_pass(runtime["workflow"])
    assert report.outcome == "ok"
    assert [step.name for step in report.steps] == ["reconcile", "writeback", "rescreen", "observe"]

    summary = runtime["store"].worker_summary()
    assert summary["last"]["outcome"] == "ok"
    assert summary["consecutive_failures"] == 0
    assert summary["outcomes"] == {"ok": 1}


def test_an_unconfirmed_payment_is_reconciled_without_a_person(runtime):
    """A lost confirmation is the case that otherwise waits forever."""
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])

    # Simulate a settlement whose confirmation never arrived: the chain has it, our record does not.
    _set_payment(
        runtime,
        runtime["legitimate_id"],
        {"confirmation_status": "UNCERTAIN", "erp_status": "PENDING"},
        WorkflowState.NEEDS_RECONCILIATION.value,
    )

    report = worker.run_pass(runtime["workflow"])
    assert _step(report, "reconcile").acted == 1
    assert runtime["store"].get_payment(runtime["legitimate_id"])["confirmation_status"] == "CONFIRMED"
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ERP_RECORDED.value


def test_a_settlement_we_could_not_read_is_reconciled_without_a_person(runtime):
    """An unreachable provider is a reason to ask again, not to hand the payment to a human."""
    _settle_then_park(runtime, confirmation_status="UNAVAILABLE")
    broadcasts = runtime["payment"].submission_calls

    report = worker.run_pass(runtime["workflow"])

    assert _step(report, "reconcile").acted == 1
    payment = runtime["store"].get_payment(runtime["legitimate_id"])
    assert payment["confirmation_status"] == "CONFIRMED"
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ERP_RECORDED.value
    # Repairing the record may not broadcast a payment that already left.
    assert runtime["payment"].submission_calls == broadcasts


def test_a_confirmation_with_no_hash_is_repaired_without_a_person(runtime):
    """The chain has it and our record has no hash for it. Asking again is the repair."""
    _settle_then_park(runtime, confirmation_status="HASH_MISSING")
    broadcasts = runtime["payment"].submission_calls

    report = worker.run_pass(runtime["workflow"])

    assert _step(report, "reconcile").acted == 1
    payment = runtime["store"].get_payment(runtime["legitimate_id"])
    assert payment["confirmation_status"] == "CONFIRMED"
    assert payment["transaction_hash"], "the missing hash is what was repaired"
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ERP_RECORDED.value
    assert runtime["payment"].submission_calls == broadcasts


def test_a_provider_that_stays_unreachable_is_examined_rather_than_silently_skipped(runtime):
    """The retry has to keep happening, and it has to stay visible while it does.

    One attempt is recorded per pass while the provider is down. That is deliberate: the pass says
    it asked, and the record says it could not get an answer.
    """
    _settle_then_park(runtime, confirmation_status="UNAVAILABLE")

    def unreachable(payment):
        raise RuntimeError("provider is down")

    runtime["payment"].inspect_payment = unreachable
    report = worker.run_pass(runtime["workflow"])

    reconcile = _step(report, "reconcile")
    assert reconcile.examined == 1
    assert reconcile.skipped == 0
    assert reconcile.acted == 1
    payment = runtime["store"].get_payment(runtime["legitimate_id"])
    assert payment["confirmation_status"] == "UNAVAILABLE"
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.NEEDS_RECONCILIATION.value
    attempts = [event for event in runtime["store"].events(runtime["legitimate_id"]) if event["type"] == "PAYMENT_RECONCILIATION_UNAVAILABLE"]
    assert len(attempts) == 1


def _settle_then_park(runtime, *, confirmation_status: str) -> None:
    """Settle a payment for real, then put our record back into the state under test.

    The provider keeps the confirmed submission, so a reconcile finds the truth the record lost.
    """
    runtime["payment"].fee_units = 10_000
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])
    _set_payment(
        runtime,
        runtime["legitimate_id"],
        {"confirmation_status": confirmation_status, "erp_status": "PENDING"},
        WorkflowState.NEEDS_RECONCILIATION.value,
    )


def test_a_lost_ledger_response_is_reconciled_once_the_claim_expires(runtime):
    """A response that was lost is not a refusal: the write may have committed, so the claim is
    kept until it expires and the next pass resolves what actually happened."""
    runtime["payment"].fee_units = 10_000
    runtime["accounting"].unsure_next_fee_write = True
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])
    assert runtime["store"].get_payment(runtime["legitimate_id"])["erp_fee_status"] == "UNKNOWN"

    # While the claim is live, the pass waits instead of racing the call it cannot see.
    waiting = worker.run_pass(runtime["workflow"])
    assert _step(waiting, "writeback").acted == 0
    assert _step(waiting, "writeback").detail["waiting"] == 1

    # Once it expires, the same pass finds the entry the lost response was hiding.
    report = worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))
    assert _step(report, "writeback").acted == 1
    payment = runtime["store"].get_payment(runtime["legitimate_id"])
    assert payment["erp_fee_status"] == "RECORDED"
    assert payment["erp_attempts"] == 0
    assert payment["erp_next_attempt_at"] is None
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ERP_RECORDED.value


def test_a_failed_writeback_backs_off_instead_of_retrying_every_pass(runtime):
    """A broken ledger must not be hammered once per pass, and it must not be forgotten either."""
    runtime["payment"].fee_units = 10_000
    # A definite refusal: nothing was written, so retrying immediately would only repeat it.
    runtime["accounting"].fail_next_fee_write = True
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])

    payment = runtime["store"].get_payment(runtime["legitimate_id"])
    assert payment["erp_fee_status"] == "FAILED"
    assert payment["erp_attempts"] == 1
    first_due = datetime.fromisoformat(payment["erp_next_attempt_at"])
    assert timedelta(seconds=50) < (first_due - utcnow()) <= timedelta(seconds=60)

    report = worker.run_pass(runtime["workflow"])
    writeback = _step(report, "writeback")
    assert writeback.acted == 0
    assert writeback.detail["waiting"] == 1
    assert writeback.detail["waiting_until"][0]["attempts"] == 1
    # Still FAILED, so the retry is deferred rather than lost.
    assert runtime["store"].get_payment(runtime["legitimate_id"])["erp_fee_status"] == "FAILED"

    # A second failure doubles the wait. The schedule is the service's, on the real clock; only the
    # worker's view of time was moved forward in this test.
    runtime["accounting"].fail_next_fee_write = True
    worker.run_pass(runtime["workflow"], now=lambda: utcnow() + timedelta(minutes=5))
    second = runtime["store"].get_payment(runtime["legitimate_id"])
    assert second["erp_attempts"] == 2
    doubled = datetime.fromisoformat(second["erp_next_attempt_at"]) - utcnow()
    assert timedelta(seconds=100) < doubled <= timedelta(seconds=120)


def test_the_backoff_schedule_is_the_one_documented():
    from arc_payables.domain import retry_backoff_seconds

    assert retry_backoff_seconds(0) == 0
    assert [retry_backoff_seconds(n) for n in range(1, 8)] == [60, 120, 240, 480, 960, 1920, 3600]
    assert retry_backoff_seconds(99) == 3600          # capped
    assert retry_backoff_seconds(3, base=10, cap=30) == 30


def test_one_broken_invoice_does_not_stop_the_rest(runtime, monkeypatch):
    """The worker has to survive its worst row, or the operator believes it is running."""
    second_id = _extra_eligible_invoice(runtime)
    for invoice_id in (runtime["legitimate_id"], second_id):
        runtime["workflow"].evaluate(invoice_id)
        runtime["workflow"].submit_payment(invoice_id)
        _set_payment(runtime, invoice_id, {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    original = APWorkflow.submit_payment

    def flaky(self, invoice_id):
        if invoice_id == runtime["legitimate_id"]:
            raise RuntimeError("provider exploded")
        return original(self, invoice_id)

    monkeypatch.setattr(APWorkflow, "submit_payment", flaky)

    report = worker.run_pass(runtime["workflow"])
    reconcile = _step(report, "reconcile")
    assert reconcile.examined == 2
    assert reconcile.acted == 1
    assert reconcile.failed == 1
    assert "provider exploded" in reconcile.detail["errors"][0]["error"]
    # A degraded pass is not a failed one: the work that could be done was done.
    assert report.outcome == "degraded"
    assert runtime["store"].get_payment(second_id)["confirmation_status"] == "CONFIRMED"
    assert runtime["store"].get_state(second_id) == WorkflowState.ERP_RECORDED.value


def test_autopay_is_off_unless_it_is_asked_for(runtime):
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    assert runtime["store"].get_state(runtime["legitimate_id"]) == WorkflowState.ELIGIBLE.value

    worker.run_pass(runtime["workflow"])
    assert runtime["store"].get_payment(runtime["legitimate_id"]) is None

    report = worker.run_pass(runtime["workflow"], autopay=True)
    assert _step(report, "autopay").acted == 1
    assert runtime["store"].get_payment(runtime["legitimate_id"])["confirmation_status"] == "CONFIRMED"


def test_autopay_pays_nothing_the_policy_did_not_authorize(runtime):
    """An escalated invoice is not eligible, so the worker cannot use this to bypass review."""
    runtime["workflow"].evaluate(runtime["suspicious_id"])
    assert runtime["store"].get_state(runtime["suspicious_id"]) != WorkflowState.ELIGIBLE.value

    report = worker.run_pass(runtime["workflow"], autopay=True)
    assert _step(report, "autopay").examined == 0
    assert runtime["store"].get_payment(runtime["suspicious_id"]) is None


def test_a_pass_stays_inside_its_action_budget(runtime):
    second_id = _extra_eligible_invoice(runtime)
    for invoice_id in (runtime["legitimate_id"], second_id):
        runtime["workflow"].evaluate(invoice_id)
        runtime["workflow"].submit_payment(invoice_id)
        _set_payment(runtime, invoice_id, {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    report = worker.run_pass(runtime["workflow"], max_actions=1)
    reconcile = _step(report, "reconcile")
    assert reconcile.examined == 2
    assert reconcile.acted == 1
    assert reconcile.detail["deferred"] == 1

    # The next pass picks up what was deferred, so a cap slows the loop without losing work.
    second = worker.run_pass(runtime["workflow"], max_actions=1)
    assert _step(second, "reconcile").acted == 1
    assert _step(second, "reconcile").detail.get("deferred", 0) == 0


def test_a_workflow_refusal_is_reported_as_a_refusal_not_a_crash(runtime, monkeypatch):
    """Losing a race is normal. The worker should count it and move on."""
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])
    _set_payment(runtime, runtime["legitimate_id"], {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    def refusing(self, invoice_id):
        raise WorkflowError(409, "payment_already_recorded", "Someone else finished this first.")

    monkeypatch.setattr(APWorkflow, "submit_payment", refusing)
    report = worker.run_pass(runtime["workflow"])
    reconcile = _step(report, "reconcile")
    assert reconcile.failed == 1
    assert reconcile.detail["refusals"] == {"payment_already_recorded": 1}
    assert "errors" not in reconcile.detail


def test_rescreen_can_be_switched_off(runtime):
    report = worker.run_pass(runtime["workflow"], rescreen=False)
    assert [step.name for step in report.steps] == ["reconcile", "writeback", "observe"]


def test_observation_reports_the_reserve_headroom(runtime):
    report = worker.run_pass(runtime["workflow"])
    observe = _step(report, "observe")
    # The demo runtime holds 5,000 USDC against a 2,000 USDC floor.
    assert observe.detail["treasury_usdc"] == 5000
    assert observe.detail["headroom_usdc"] == 3000
    assert "warning" not in observe.detail


def test_observation_warns_when_the_floor_is_breached(runtime):
    runtime["workflow"].settings.min_reserve_usdc = 6000
    observe = _step(worker.run_pass(runtime["workflow"]), "observe")
    assert observe.detail["headroom_usdc"] == -1000
    assert observe.detail["warning"] == "the treasury is below the reserve floor"


def test_an_unreadable_invoice_table_ends_the_pass_without_lying(runtime, monkeypatch):
    def broken():
        raise RuntimeError("database is gone")

    monkeypatch.setattr(runtime["store"], "list_invoices", broken)
    report = worker.run_pass(runtime["workflow"])
    assert report.outcome == "failed"
    assert "database is gone" in report.stopped_reason
    # The attempt is still recorded, so the failure is visible from another process.
    assert runtime["store"].worker_summary()["last"]["outcome"] == "failed"


def test_the_loop_keeps_going_after_a_bad_pass(monkeypatch, runtime):
    passes = []
    stop = threading.Event()

    def exploding(*args, **kwargs):
        passes.append("attempted")
        raise RuntimeError("pass blew up")

    def sleep(_seconds):
        stop.set()  # one iteration, then stop
        return True

    monkeypatch.setattr(worker, "run_pass", exploding)
    worker.run_forever(runtime["workflow"], interval_seconds=0, stop_event=stop, sleep=sleep)
    assert passes == ["attempted"]


def test_the_loop_stops_when_asked(runtime):
    outcomes = []
    waits = []
    stop = threading.Event()

    def sleep(_seconds):
        waits.append(1)
        if len(waits) >= 2:
            stop.set()
        return stop.is_set()

    worker.run_forever(
        runtime["workflow"],
        interval_seconds=0,
        stop_event=stop,
        sleep=sleep,
        on_pass=lambda report: outcomes.append(report.outcome),
    )
    assert len(waits) == 2
    assert outcomes == ["ok", "ok"]


def test_consecutive_failures_reset_after_a_good_pass(runtime):
    from arc_payables.domain import utcnow

    store = runtime["store"]
    now = utcnow().isoformat()
    store.record_worker_run(now, now, "degraded", {})
    store.record_worker_run(now, now, "degraded", {})
    assert store.worker_summary()["consecutive_failures"] == 2
    store.record_worker_run(now, now, "ok", {})
    assert store.worker_summary()["consecutive_failures"] == 0
    assert store.worker_summary()["outcomes"] == {"ok": 1, "degraded": 2}


def test_a_pass_records_the_alerts_it_would_raise(runtime):
    """Alerts are part of the pass record even with no destination, so a scrape or a console sees them."""
    runtime["workflow"].settings.min_reserve_usdc = 6000  # balance is 5,000
    report = worker.run_pass(runtime["workflow"])
    codes = {alert["code"] for alert in report.alerts}
    assert "reserve_breached" in codes

    recorded = runtime["store"].worker_summary()["last"]["detail"]
    assert {alert["code"] for alert in recorded["alerts"]} == codes


def test_a_pass_delivers_alerts_when_a_destination_is_configured(runtime):
    from arc_payables.alerting import WebhookSink

    runtime["workflow"].settings.min_reserve_usdc = 6000
    sent = []
    sink = WebhookSink(
        "https://alerts.example.test/hook",
        poster=lambda url, payload: sent.append((url, payload)),
    )
    worker.run_pass(runtime["workflow"], alerts_sink=sink)
    assert len(sent) == 1
    assert sent[0][0] == "https://alerts.example.test/hook"
    assert sent[0][1]["alerts"][0]["severity"] == "critical"


def test_a_streak_of_degraded_passes_crosses_the_alert_threshold(runtime, monkeypatch):
    """The threshold counts the pass being reported, not just the ones already recorded."""
    second_id = _extra_eligible_invoice(runtime)
    for invoice_id in (runtime["legitimate_id"], second_id):
        runtime["workflow"].evaluate(invoice_id)
        runtime["workflow"].submit_payment(invoice_id)
        _set_payment(runtime, invoice_id, {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)

    def broken(self, invoice_id):
        raise RuntimeError("provider is down")

    monkeypatch.setattr(APWorkflow, "submit_payment", broken)
    threshold = runtime["workflow"].settings.alert_after_consecutive_failures
    assert threshold >= 2

    seen = []
    for _ in range(threshold):
        report = worker.run_pass(runtime["workflow"])
        assert report.outcome == "degraded"
        seen.append({alert["code"] for alert in report.alerts})

    assert all("worker_failing" not in codes for codes in seen[:-1])
    assert "worker_failing" in seen[-1]
    assert runtime["store"].worker_summary()["consecutive_failures"] == threshold
