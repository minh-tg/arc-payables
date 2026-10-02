"""The background pass: what happens when nobody is asking.

Every other entry point in this project is a person or a test calling something. This is the part
that runs on a timer, and it exists because three things otherwise wait forever:

* a settlement whose confirmation never came back stays unconfirmed,
* a confirmed payment the ledger rejected stays unrecorded,
* a screening taken months ago stays at its old risk tier.

A pass does the work it can do without a decision, records what it did, and stops. It never approves
anything, and by default it never starts a payment either: paying unattended is opt-in, because that
is a policy an operator should state rather than inherit.

Isolation is the point. One invoice that raises must not stop the other nine, one broken step must
not stop the pass, and a failing pass must not stop the loop. A worker that dies on its first bad
row is worse than no worker, because the operator believes it is running.
"""

from __future__ import annotations

import signal
import sys
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from .domain import DecisionAction, utcnow
from .service import APWorkflow, WorkflowError

MAX_ERROR_CHARS = 200
"""Errors go into a database column and a metric label, so they are truncated rather than trusted."""

RESUMABLE_CONFIRMATIONS = {"UNCERTAIN", "PENDING"}
"""Settled states that are not final. `submit_payment` resumes these without re-authorizing."""

@dataclass
class StepReport:
    """What one step of a pass did."""

    name: str
    examined: int = 0
    acted: int = 0
    failed: int = 0
    skipped: int = 0
    detail: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.failed == 0 and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "examined": self.examined,
            "acted": self.acted,
            "failed": self.failed,
            "skipped": self.skipped,
            "detail": self.detail,
            "error": self.error,
        }


@dataclass
class PassReport:
    """What a whole pass did, and whether it finished cleanly."""

    started_at: datetime
    finished_at: datetime
    steps: list[StepReport]
    stopped_reason: str | None = None
    alerts: list[dict[str, Any]] = field(default_factory=list)
    alert_delivery: list[dict[str, Any]] = field(default_factory=list)

    @property
    def failed_steps(self) -> list[StepReport]:
        return [step for step in self.steps if not step.ok]

    @property
    def outcome(self) -> str:
        if self.stopped_reason:
            return "failed"
        return "ok" if not self.failed_steps else "degraded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat(),
            "outcome": self.outcome,
            "stopped_reason": self.stopped_reason,
            "steps": [step.to_dict() for step in self.steps],
            "alerts": self.alerts,
            "alert_delivery": self.alert_delivery,
        }

    def summary_line(self) -> str:
        parts = [
            f"{step.name}={step.acted}/{step.examined}" + (f" ({step.failed} failed)" if step.failed else "")
            for step in self.steps
        ]
        return f"pass {self.outcome}: " + ", ".join(parts)


def _bounded(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}"
    return text[:MAX_ERROR_CHARS]


def run_pass(
    workflow: APWorkflow,
    *,
    autopay: bool = False,
    intake: bool = False,
    rescreen: bool = True,
    max_actions: int = 25,
    alerts_sink: Any | None = None,
    now: Callable[[], datetime] = utcnow,
) -> PassReport:
    """Run one pass over every invoice, then the standing jobs. Never raises for one bad row."""
    started = now()
    report = PassReport(started_at=started, finished_at=started, steps=[])

    try:
        invoices = workflow.store.list_invoices()
    except Exception as exc:
        report.stopped_reason = f"could not read invoices: {_bounded(exc)}"
        report.finished_at = now()
        _record(workflow, report)
        return report

    remaining = max_actions
    for step_name, needs_action, handler in (
        ("reconcile", _needs_reconcile, _reconcile_payment),
        ("writeback", _needs_writeback, _retry_writeback),
    ):
        step = StepReport(name=step_name)
        for invoice, state in invoices:
            payment = workflow.store.get_payment(invoice.id)
            if payment is None:
                continue
            waiting = _backoff_remaining(payment, now())
            if waiting is not None:
                step.detail["waiting"] = step.detail.get("waiting", 0) + 1
                step.detail.setdefault("waiting_until", []).append(
                    {"invoice_id": invoice.id, "seconds": waiting, "attempts": int(payment.get("erp_attempts") or 0)}
                )
                continue
            if not needs_action(payment, state):
                step.skipped += 1
                continue
            step.examined += 1
            if remaining <= 0:
                step.detail["deferred"] = step.detail.get("deferred", 0) + 1
                continue
            try:
                handler(workflow, invoice)
                remaining -= 1
                step.acted += 1
            except WorkflowError as exc:
                # A workflow refusal is an answer, not a fault: the invoice was already settled, or
                # is being written by someone else right now.
                step.failed += 1
                step.detail.setdefault("refusals", {})
                step.detail["refusals"][exc.code] = step.detail["refusals"].get(exc.code, 0) + 1
            except Exception as exc:
                step.failed += 1
                step.detail.setdefault("errors", [])
                if len(step.detail["errors"]) < 5:
                    step.detail["errors"].append({"invoice_id": invoice.id, "error": _bounded(exc)})
        report.steps.append(step)

    if intake:
        # Discovery runs before autopay so a payable found in this pass can be decided and paid in
        # the same one, and always after the two repair steps, which owe nothing to the queue.
        report.steps.append(_intake(workflow, remaining))
        # Intake adds invoices, so autopay reads the queue again rather than the pre-intake list.
        invoices = workflow.store.list_invoices()

    if autopay:
        report.steps.append(_autopay(workflow, invoices, remaining))

    if rescreen:
        report.steps.append(_rescreen(workflow))

    report.steps.append(_observe(workflow))

    report.finished_at = now()
    _raise_alerts(workflow, report, alerts_sink)
    _record(workflow, report)
    return report


def _backoff_remaining(payment: dict, now: datetime) -> int | None:
    """Seconds until this payment's writeback is worth retrying, if it is in backoff.

    A definite failure schedules the next attempt; an uncertain one does not, because the write may
    have committed and finding out is urgent.
    """
    next_attempt = payment.get("erp_next_attempt_at")
    if not next_attempt:
        return None
    from .monitoring import _parse  # one date parser, already written and tested

    due = _parse(str(next_attempt))
    if due is None:
        return None
    remaining = int((due - now).total_seconds())
    return remaining if remaining > 0 else None


def _raise_alerts(workflow: APWorkflow, report: PassReport, sink: Any | None) -> None:
    """Work out what is worth telling someone, record it on the pass, and send if configured."""
    from .alerting import evaluate_alerts, log_alerts
    from .metrics import collect

    try:
        snapshot = collect(workflow)
    except Exception as exc:  # pragma: no cover - collect reads defensively already
        report.alert_delivery.append({"code": "snapshot_failed", "error": _bounded(exc)})
        return
    # The pass being reported is not recorded yet, so it is added to the streak the store knows.
    streak = snapshot["worker_consecutive_failures"]
    consecutive = 0 if report.outcome == "ok" else streak + 1
    alerts = evaluate_alerts(
        outcome=report.outcome,
        consecutive_failures=consecutive,
        snapshot=snapshot,
        settings=workflow.settings,
    )
    report.alerts = [alert.to_dict() for alert in alerts]
    if not alerts:
        return
    log_alerts(alerts)
    if sink is None:
        return
    report.alert_delivery = sink.send(alerts)


def _needs_reconcile(payment: dict, state: str) -> bool:
    from .domain import WorkflowState

    if state in {WorkflowState.ERP_RECORDED.value}:
        return False
    return str(payment.get("confirmation_status") or "") in RESUMABLE_CONFIRMATIONS


#: A writeback disabled from configuration cannot be retried into working: every attempt writes an
#: event and changes nothing. These are the only disables the worker skips. Everything else that
#: disabled a writeback was a provider condition, and the provider is the thing worth asking again.
#: Treating them alike is how a payment settled on chain stayed unbooked with no path back.
PERMANENT_WRITEBACK_DISABLES = frozenset({"ACCOUNTING_MAPPING_INCOMPLETE"})


def _needs_writeback(payment: dict, state: str) -> bool:
    """Whether the ledger is still missing something for a settled payment.

    The state answers this, not one status field. A writeback has two documents, so the payment entry
    can be recorded while the network-fee entry is not, and a predicate that only reads `erp_status`
    sees "RECORDED" and skips the very invoice that needs finishing.

    A disable is not automatically final either. `NETWORK_FEE_UNAVAILABLE` means the provider could
    not name the fee at that moment, which is a condition that can pass; only a configuration
    problem is skipped outright.
    """
    from .domain import WorkflowState

    if state == WorkflowState.ERP_RECORDED.value:
        return False
    if payment.get("confirmation_status") != "CONFIRMED":
        return False
    if payment.get("erp_error_code") in PERMANENT_WRITEBACK_DISABLES:
        return False
    return True


def _reconcile_payment(workflow: APWorkflow, invoice) -> None:
    """Ask again what happened to a settlement we could not confirm. Idempotent by design."""
    workflow.submit_payment(invoice.id)


def _retry_writeback(workflow: APWorkflow, invoice) -> None:
    workflow.retry_erp_writeback(invoice.id)


def _intake(workflow: APWorkflow, remaining: int) -> StepReport:
    """Bring payables the ledger still owes into the local queue, then evaluate them.

    Discovery decides only what to look at. Everything after it is the same path a person would
    take by hand: the connector imports the payable, and the deterministic policy decides. The
    worker has no approval path, so an invoice that fails a check escalates for a human, and
    payment stays the separate autopay decision.
    """
    step = StepReport(name="intake")
    lister = getattr(workflow.accounting, "list_open_payables", None)
    if lister is None:
        step.skipped += 1
        step.detail["reason"] = "the accounting connector cannot enumerate payables"
        return step
    try:
        external_ids = lister()
    except Exception as exc:
        step.error = _bounded(exc)
        step.failed += 1
        return step

    known = {invoice.purchase_invoice_id for invoice, _state in workflow.store.list_invoices()}
    for external_id in external_ids:
        if not external_id or external_id in known:
            step.skipped += 1
            continue
        step.examined += 1
        if remaining <= 0:
            step.detail["deferred"] = step.detail.get("deferred", 0) + 1
            continue
        try:
            invoice, _is_new = workflow.import_invoice(str(external_id), _intake_key(str(external_id)))
            workflow.evaluate(invoice.id)
            known.add(str(external_id))
            remaining -= 1
            step.acted += 1
        except WorkflowError as exc:
            # A refusal is an answer, not a fault. The ledger can hold payables this system will not
            # settle: a foreign currency it has no rate for, a duplicate invoice number, a record
            # that vanished between the listing and the read. Declining the same ones every pass is
            # not a failure worth alerting on, so the reason is recorded for the operator instead.
            step.skipped += 1
            step.detail.setdefault("declined_codes", {})
            step.detail["declined_codes"][exc.code] = step.detail["declined_codes"].get(exc.code, 0) + 1
            step.detail.setdefault("declined", [])
            if len(step.detail["declined"]) < 5:
                step.detail["declined"].append({"external_id": external_id, "code": exc.code, "reason": _reason(exc)})
        except Exception as exc:
            step.failed += 1
            step.detail.setdefault("errors", [])
            if len(step.detail["errors"]) < 5:
                step.detail["errors"].append({"external_id": external_id, "error": _bounded(exc)})
    return step


def _reason(exc: BaseException) -> str:
    """What the workflow said, plus the cause it wrapped, bounded for the record.

    ``import_invoice`` flattens every connector failure into one code, so without the cause an
    operator sees 'accounting_read_failed' and cannot tell a missing FX rate from an outage.
    """
    cause = exc.__cause__
    text = str(exc) if cause is None else f"{exc} Cause: {type(cause).__name__}: {cause}"
    return text[:MAX_ERROR_CHARS]


def _intake_key(external_id: str) -> str:
    """A stable idempotency key per payable, so a repeated pass cannot capture the same one twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"arc-payables:intake:{external_id}"))


def _autopay(workflow: APWorkflow, invoices, remaining: int) -> StepReport:
    """Pay what the deterministic policy already authorized. Nothing else, ever.

    Only an invoice the policy itself put in `ELIGIBLE` with a `PAY_NOW` decision is paid. An
    escalated or held invoice is not touched, so this cannot become a way to bypass review.
    """
    from .domain import WorkflowState

    step = StepReport(name="autopay")
    for invoice, state in invoices:
        if state != WorkflowState.ELIGIBLE.value:
            continue
        if workflow.store.get_payment(invoice.id) is not None:
            continue
        decision = workflow.store.get_decision(invoice.id) or {}
        if decision.get("action") != DecisionAction.PAY_NOW.value:
            continue
        step.examined += 1
        if remaining <= 0:
            step.detail["deferred"] = step.detail.get("deferred", 0) + 1
            continue
        try:
            workflow.submit_payment(invoice.id)
            remaining -= 1
            step.acted += 1
        except WorkflowError as exc:
            step.failed += 1
            step.detail.setdefault("refusals", {})
            step.detail["refusals"][exc.code] = step.detail["refusals"].get(exc.code, 0) + 1
        except Exception as exc:
            step.failed += 1
            step.detail.setdefault("errors", [])
            if len(step.detail["errors"]) < 5:
                step.detail["errors"].append({"invoice_id": invoice.id, "error": _bounded(exc)})
    return step


def _rescreen(workflow: APWorkflow) -> StepReport:
    """Re-screen counterparties that are past their cadence. The cadence lives in monitoring."""
    step = StepReport(name="rescreen")
    try:
        from .monitoring import rescreen_suppliers

        outcomes = rescreen_suppliers(workflow, source="worker")
        step.examined = len(outcomes)
        for outcome in outcomes:
            detail = outcome.to_dict() if hasattr(outcome, "to_dict") else dict(outcome)
            if detail.get("rechecked"):
                step.acted += 1
            else:
                step.skipped += 1
    except Exception as exc:
        step.error = _bounded(exc)
        step.failed += 1
    return step


def _observe(workflow: APWorkflow) -> StepReport:
    """Report what the money looks like. Observation is not evidence, so this writes nothing.

    The audit chain belongs to decisions. A balance reading is not one, and putting it there would
    make the chain noisy and the metrics depend on a write.
    """
    step = StepReport(name="observe")
    try:
        balance_units = workflow.payment_provider.get_balance().balance_units
    except Exception as exc:
        step.error = _bounded(exc)
        step.failed += 1
        return step
    reserve_units = workflow.settings.min_reserve_units
    step.detail = {
        "treasury_usdc": round(balance_units / 1_000_000, 6),
        "reserve_usdc": float(workflow.settings.min_reserve_usdc),
        "headroom_usdc": round((balance_units - reserve_units) / 1_000_000, 6),
    }
    if balance_units < reserve_units:
        step.detail["warning"] = "the treasury is below the reserve floor"
    if hasattr(workflow.payment_provider, "guard_limits"):
        limits = workflow.payment_provider.guard_limits()
        if limits:
            step.detail["guard"] = {key: round(value / 1_000_000, 6) for key, value in limits.items()}
    return step


def _record(workflow: APWorkflow, report: PassReport) -> None:
    """Persist the pass so the API process can report on a worker it cannot see."""
    try:
        workflow.store.record_worker_run(
            report.started_at.isoformat(), report.finished_at.isoformat(), report.outcome, report.to_dict()
        )
    except Exception as exc:  # pragma: no cover - defensive: a pass already did its work
        print(f"worker: could not record the pass: {_bounded(exc)}", file=sys.stderr)


def run_forever(
    workflow: APWorkflow,
    *,
    interval_seconds: float,
    autopay: bool = False,
    intake: bool = False,
    rescreen: bool = True,
    max_actions: int = 25,
    stop_event: threading.Event | None = None,
    sleep: Callable[[float], bool] | None = None,
    on_pass: Callable[[PassReport], None] | None = None,
) -> None:
    """Run passes until interrupted. A failing pass is reported and the loop continues.

    `sleep` and `on_pass` are injectable so the loop can be tested without waiting and without
    capturing stdout.
    """
    from .alerting import WebhookSink

    stop = stop_event or threading.Event()
    waiter = sleep or (lambda seconds: stop.wait(seconds))
    sink = _build_sink(workflow)
    while not stop.is_set():
        try:
            report = run_pass(workflow, autopay=autopay, intake=intake, rescreen=rescreen, max_actions=max_actions, alerts_sink=sink)
        except Exception as exc:  # pragma: no cover - a pass catches its own step failures
            print(f"worker: pass aborted: {_bounded(exc)}", file=sys.stderr)
        else:
            print(report.summary_line(), flush=True)
            if on_pass is not None:
                on_pass(report)
        if waiter(interval_seconds):
            break


def _build_sink(workflow: APWorkflow) -> Any | None:
    """The configured destination, or nothing. An unconfigured loop still records its alerts."""
    url = getattr(workflow.settings, "alert_webhook_url", None)
    if not url:
        return None
    from .alerting import WebhookSink

    return WebhookSink(url, min_interval_seconds=workflow.settings.alert_min_interval_seconds)


def _cli_or_setting(cli_value: bool | None, configured_value: bool) -> bool:
    """Let an explicit CLI choice override configuration, otherwise use the configured default."""
    return configured_value if cli_value is None else cli_value


def _build_parser():
    import argparse

    parser = argparse.ArgumentParser(description="Run the arc-payables background worker.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit; exit 1 if it did not finish cleanly")
    parser.add_argument("--interval", type=float, default=None, help="Seconds between passes (default: WORKER_INTERVAL_SECONDS)")
    parser.add_argument(
        "--autopay",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Pay invoices the policy already authorized (overrides WORKER_AUTOPAY)",
    )
    parser.add_argument(
        "--intake",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Import and evaluate payables the ledger still owes (overrides WORKER_INTAKE)",
    )
    parser.add_argument("--no-rescreen", action="store_true", help="Skip re-screening in this process")
    parser.add_argument("--max-actions", type=int, default=None, help="Cap actions per pass (default: WORKER_MAX_ACTIONS_PER_PASS)")
    return parser


def main() -> None:
    from .runtime import build_workflow
    from .settings import get_settings

    args = _build_parser().parse_args()
    settings = get_settings()
    # One process, one workflow: the same wiring the API uses, without importing the HTTP app.
    workflow = build_workflow(settings)
    autopay = _cli_or_setting(args.autopay, settings.worker_autopay)
    intake = _cli_or_setting(args.intake, settings.worker_intake)

    if args.once:
        report = run_pass(
            workflow,
            autopay=autopay,
            intake=intake,
            rescreen=not args.no_rescreen,
            max_actions=args.max_actions or settings.worker_max_actions_per_pass,
            alerts_sink=_build_sink(workflow),
        )
        print(report.summary_line())
        raise SystemExit(0 if report.outcome == "ok" else 1)

    stop = threading.Event()

    def handle_signal(signum, _frame):  # pragma: no cover - signal handling
        print(f"worker: signal {signum}, finishing the current pass", flush=True)
        stop.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), handle_signal)

    interval = args.interval or settings.worker_interval_seconds
    print(f"worker: every {interval:g}s, autopay={'on' if autopay else 'off'}, intake={'on' if intake else 'off'}", flush=True)
    run_forever(
        workflow,
        interval_seconds=interval,
        autopay=autopay,
        intake=intake,
        rescreen=not args.no_rescreen,
        max_actions=args.max_actions or settings.worker_max_actions_per_pass,
        stop_event=stop,
    )
