"""Operational metrics, rendered in the Prometheus text format.

Two halves, on purpose. :func:`collect` gathers a plain snapshot and :func:`render` formats it, so
the numbers can be asserted without parsing text that a scraper will parse.

Labels are kept bounded. States, statuses, kinds and outcomes come from small vocabularies; supplier
names and invoice ids deliberately do not appear, because a label whose value grows with the data
is how a metrics endpoint becomes a memory leak.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .domain import USDC_SCALE, utcnow
from .service import APWorkflow

STUCK_CONFIRMATION_STATES = {"UNCERTAIN", "PENDING"}
"""A settled payment we have not confirmed. It needs reconciliation, and nothing else will do it."""

STUCK_ERP_STATES = {"PENDING", "UNKNOWN"}
"""A confirmed payment the ledger has not accepted yet. Retryable, and it should not need a person."""


def _usdc(units: int | None) -> float:
    if units is None:
        return 0.0
    return round(units / USDC_SCALE, 6)


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def collect(workflow: APWorkflow, *, now: datetime | None = None) -> dict[str, Any]:
    """Gather the snapshot. Every value here is read from the store or the providers, never guessed."""
    store = workflow.store
    now = now or utcnow()
    invoices = store.list_invoices()

    invoice_states: dict[str, int] = {}
    confirmations: dict[str, int] = {}
    erp_statuses: dict[str, int] = {}
    uncertain = 0
    stuck_erp = 0
    fee_units_total = 0
    for invoice, state in invoices:
        invoice_states[state] = invoice_states.get(state, 0) + 1
        payment = store.get_payment(invoice.id)
        if not payment:
            continue
        confirmation = str(payment.get("confirmation_status") or "NONE")
        confirmations[confirmation] = confirmations.get(confirmation, 0) + 1
        if confirmation in STUCK_CONFIRMATION_STATES:
            uncertain += 1
        erp_status = str(payment.get("erp_status") or "NONE")
        erp_statuses[erp_status] = erp_statuses.get(erp_status, 0) + 1
        if confirmation == "CONFIRMED" and erp_status in STUCK_ERP_STATES:
            stuck_erp += 1
        if payment.get("fee_units") is not None:
            fee_units_total += int(payment["fee_units"])

    # Treasury and the contract's own budgets, when the provider can report them.
    balance_units: int | None = None
    guard: dict[str, int] | None = None
    try:
        balance_units = workflow.payment_provider.get_balance().balance_units
    except Exception:
        balance_units = None
    if hasattr(workflow.payment_provider, "guard_limits"):
        try:
            guard = workflow.payment_provider.guard_limits()
        except Exception:
            guard = None

    # Screening ages, and how many counterparties are past their cadence.
    from .monitoring import open_invoices_by_supplier

    by_supplier = open_invoices_by_supplier(workflow)
    ages: list[float] = []
    due = 0
    cadence_seconds = int(getattr(workflow.settings, "rescreen_interval_hours", 24)) * 3600
    for supplier_id in by_supplier:
        row = store.latest_screening(supplier_id)
        checked = _parse(row.get("checked_at")) if row else None
        if checked is None:
            due += 1
            continue
        age = (now - checked).total_seconds()
        ages.append(age)
        if age >= cadence_seconds:
            due += 1

    audit = store.verify_audit_chain()
    worker = store.worker_summary()
    last_run = worker["last"] or {}
    last_finished = _parse(last_run.get("finished_at"))

    return {
        "invoices_total": len(invoices),
        "invoice_states": invoice_states,
        "payment_confirmations": confirmations,
        "payment_erp_statuses": erp_statuses,
        "payments_uncertain": uncertain,
        "payments_awaiting_ledger": stuck_erp,
        "payment_fee_usdc_total": _usdc(fee_units_total),
        "treasury_usdc": _usdc(balance_units),
        "treasury_available": balance_units is not None,
        "reserve_usdc": float(workflow.settings.min_reserve_usdc),
        "reserve_headroom_usdc": (_usdc(balance_units) - float(workflow.settings.min_reserve_usdc))
        if balance_units is not None
        else None,
        "guard": {k: _usdc(v) for k, v in guard.items()} if guard else None,
        "guard_paused": bool(guard.get("paused")) if guard else None,
        "screenings_due": due,
        "screening_oldest_age_seconds": round(max(ages), 3) if ages else 0.0,
        "screenings_known": len(ages),
        "audit_chain_ok": 1 if audit.get("ok") else 0,
        "audit_entries": int(audit.get("length") or 0),
        "worker_last_run_timestamp": last_finished.timestamp() if last_finished else None,
        "worker_last_outcome": last_run.get("outcome"),
        "worker_consecutive_failures": int(worker["consecutive_failures"]),
        "worker_outcomes": worker["outcomes"],
    }


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _line(name: str, value: float, labels: dict[str, Any] | None = None) -> str:
    if labels:
        rendered = ",".join(f'{key}="{_escape(str(item))}"' for key, item in sorted(labels.items()))
        return f"{name}{{{rendered}}} {value}"
    return f"{name} {value}"


def _block(name: str, help_text: str, kind: str, samples: list[str]) -> list[str]:
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {kind}", *samples]


def render(snapshot: dict[str, Any]) -> str:
    """Format a snapshot as Prometheus text exposition format 0.0.4."""
    lines: list[str] = []

    lines += _block(
        "arc_payables_invoices",
        "Invoices by workflow state.",
        "gauge",
        [_line("arc_payables_invoices", count, {"state": state}) for state, count in sorted(snapshot["invoice_states"].items())],
    )
    lines += _block(
        "arc_payables_payments",
        "Payments by settlement confirmation status.",
        "gauge",
        [
            _line("arc_payables_payments", count, {"confirmation": status})
            for status, count in sorted(snapshot["payment_confirmations"].items())
        ],
    )
    lines += _block(
        "arc_payables_payments_erp_status",
        "Payments by accounting writeback status.",
        "gauge",
        [
            _line("arc_payables_payments_erp_status", count, {"status": status})
            for status, count in sorted(snapshot["payment_erp_statuses"].items())
        ],
    )
    lines += _block(
        "arc_payables_payments_needing_attention",
        "Payments a human or the worker has to finish: unconfirmed settlements, and confirmed ones the ledger has not taken.",
        "gauge",
        [
            _line("arc_payables_payments_needing_attention", snapshot["payments_uncertain"], {"reason": "unconfirmed"}),
            _line("arc_payables_payments_needing_attention", snapshot["payments_awaiting_ledger"], {"reason": "unrecorded"}),
        ],
    )
    lines += _block(
        "arc_payables_payment_fee_usdc",
        "Network fees absorbed, in USDC, summed over recorded payments.",
        "gauge",
        [_line("arc_payables_payment_fee_usdc", snapshot["payment_fee_usdc_total"])],
    )
    lines += _block(
        "arc_payables_treasury_usdc",
        "Settlement-account balance in USDC, if the provider could be read.",
        "gauge",
        [_line("arc_payables_treasury_usdc", snapshot["treasury_usdc"])],
    )
    lines += _block(
        "arc_payables_treasury_probe_ok",
        "Whether the treasury balance was readable when this snapshot was taken.",
        "gauge",
        [_line("arc_payables_treasury_probe_ok", 1 if snapshot["treasury_available"] else 0)],
    )
    lines += _block(
        "arc_payables_reserve_usdc",
        "Configured treasury reserve floor.",
        "gauge",
        [_line("arc_payables_reserve_usdc", snapshot["reserve_usdc"])],
    )
    if snapshot["reserve_headroom_usdc"] is not None:
        lines += _block(
            "arc_payables_reserve_headroom_usdc",
            "Balance above the reserve floor. Negative means the floor is already breached.",
            "gauge",
            [_line("arc_payables_reserve_headroom_usdc", snapshot["reserve_headroom_usdc"])],
        )
    if snapshot["guard"]:
        lines += _block(
            "arc_payables_guard_limit_usdc",
            "Budgets the deployed guard enforces, read from the chain.",
            "gauge",
            [
                _line("arc_payables_guard_limit_usdc", value, {"kind": kind})
                for kind, value in sorted(snapshot["guard"].items())
                if kind in {"per_payment_cap", "epoch_cap", "recipient_epoch_cap"}
            ],
        )
        lines += _block(
            "arc_payables_guard_paused",
            "Whether the guard is paused. Any non-zero value means payments are stopped.",
            "gauge",
            [_line("arc_payables_guard_paused", 1 if snapshot["guard_paused"] else 0)],
        )
    lines += _block(
        "arc_payables_screenings_due",
        "Counterparties with open invoices whose screening is past its cadence or absent.",
        "gauge",
        [_line("arc_payables_screenings_due", snapshot["screenings_due"])],
    )
    lines += _block(
        "arc_payables_screening_oldest_age_seconds",
        "Age of the oldest screening for a counterparty with an open invoice.",
        "gauge",
        [_line("arc_payables_screening_oldest_age_seconds", snapshot["screening_oldest_age_seconds"])],
    )
    lines += _block(
        "arc_payables_audit_chain_ok",
        "Whether the audit chain verifies against the configured signer.",
        "gauge",
        [_line("arc_payables_audit_chain_ok", snapshot["audit_chain_ok"])],
    )
    lines += _block(
        "arc_payables_audit_entries",
        "Entries in the audit chain.",
        "gauge",
        [_line("arc_payables_audit_entries", snapshot["audit_entries"])],
    )
    lines += _block(
        "arc_payables_worker_consecutive_failures",
        "Passes in a row that did not finish cleanly. Alert on this, not on a single pass.",
        "gauge",
        [_line("arc_payables_worker_consecutive_failures", snapshot["worker_consecutive_failures"])],
    )
    if snapshot["worker_last_run_timestamp"] is not None:
        lines += _block(
            "arc_payables_worker_last_run_timestamp_seconds",
            "When the most recent worker pass finished.",
            "gauge",
            [
                _line(
                    "arc_payables_worker_last_run_timestamp_seconds",
                    snapshot["worker_last_run_timestamp"],
                    {"outcome": snapshot["worker_last_outcome"] or "unknown"},
                )
            ],
        )
    lines += _block(
        "arc_payables_worker_runs",
        "Worker passes by outcome, over the runs retained for health.",
        "gauge",
        [_line("arc_payables_worker_runs", count, {"outcome": outcome}) for outcome, count in sorted(snapshot["worker_outcomes"].items())],
    )
    return "\n".join(lines) + "\n"
