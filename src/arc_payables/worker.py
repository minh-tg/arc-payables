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
    rescreen: bool = True,
    max_actions: int = 25,
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

    if autopay:
        report.steps.append(_autopay(workflow, invoices, remaining))

    if rescreen:
        report.steps.append(_rescreen(workflow))

    report.steps.append(_observe(workflow))

    report.finished_at = now()
    _record(workflow, report)
    return report


def _needs_reconcile(payment: dict, state: str) -> bool:
    from .domain import WorkflowState

    if state in {WorkflowState.ERP_RECORDED.value}:
        return False
    return str(payment.get("confirmation_status") or "") in RESUMABLE_CONFIRMATIONS


def _needs_writeback(payment: dict, state: str) -> bool:
    """Whether the ledger is still missing something for a settled payment.

    The state answers this, not one status field. A writeback has two documents, so the payment entry
    can be recorded while the network-fee entry is not, and a predicate that only reads `erp_status`
    sees "RECORDED" and skips the very invoice that needs finishing.
    """
    from .domain import WorkflowState

    if state == WorkflowState.ERP_RECORDED.value:
        return False
    if payment.get("confirmation_status") != "CONFIRMED":
        return False
    # Retrying a configuration problem cannot fix it, and every attempt writes an event.
    if payment.get("erp_error_code") == "ACCOUNTING_MAPPING_INCOMPLETE":
        return False
    if "DISABLED" in {str(payment.get("erp_status") or ""), str(payment.get("erp_fee_status") or "")}:
        return False
    return True


def _reconcile_payment(workflow: APWorkflow, invoice) -> None:
    """Ask again what happened to a settlement we could not confirm. Idempotent by design."""
    workflow.submit_payment(invoice.id)


def _retry_writeback(workflow: APWorkflow, invoice) -> None:
    workflow.retry_erp_writeback(invoice.id)


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
    stop = stop_event or threading.Event()
    waiter = sleep or (lambda seconds: stop.wait(seconds))
    while not stop.is_set():
        try:
            report = run_pass(workflow, autopay=autopay, rescreen=rescreen, max_actions=max_actions)
        except Exception as exc:  # pragma: no cover - a pass catches its own step failures
            print(f"worker: pass aborted: {_bounded(exc)}", file=sys.stderr)
        else:
            print(report.summary_line(), flush=True)
            if on_pass is not None:
                on_pass(report)
        if waiter(interval_seconds):
            break


def main() -> None:
    import argparse

    from .api import create_app
    from .settings import get_settings

    parser = argparse.ArgumentParser(description="Run the arc-payables background worker.")
    parser.add_argument("--once", action="store_true", help="Run a single pass and exit; exit 1 if it did not finish cleanly")
    parser.add_argument("--interval", type=float, default=None, help="Seconds between passes (default: WORKER_INTERVAL_SECONDS)")
    parser.add_argument("--autopay", action="store_true", help="Also pay invoices the policy already authorized")
    parser.add_argument("--no-rescreen", action="store_true", help="Skip re-screening in this process")
    parser.add_argument("--max-actions", type=int, default=None, help="Cap actions per pass (default: WORKER_MAX_ACTIONS_PER_PASS)")
    args = parser.parse_args()

    settings = get_settings()
    # One process, one workflow: the same wiring the API uses, so a pass can never see different
    # providers or a different policy from the service that serves the console.
    workflow = create_app(settings=settings).state.workflow

    if args.once:
        report = run_pass(
            workflow,
            autopay=args.autopay,
            rescreen=not args.no_rescreen,
            max_actions=args.max_actions or settings.worker_max_actions_per_pass,
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
    print(f"worker: every {interval:g}s, autopay={'on' if args.autopay else 'off'}", flush=True)
    run_forever(
        workflow,
        interval_seconds=interval,
        autopay=args.autopay,
        rescreen=not args.no_rescreen,
        max_actions=args.max_actions or settings.worker_max_actions_per_pass,
        stop_event=stop,
    )
