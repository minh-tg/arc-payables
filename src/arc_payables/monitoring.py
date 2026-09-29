"""Continuous counterparty monitoring.

Screening is usually a single check at onboarding, which is why a sanctions list that grows is
worth less than it should be. This re-screens existing counterparties on a schedule, keeps the
history, and records a change of risk profile on the audit chain of every open invoice it
affects — so a payment record shows not only what was checked, but when the counterparty's risk
changed under it.

Screening a counterparty never authorizes anything: the result flows into the same evidence and
the same policy as before, where its tier scales the automatic limit and a flagged result still
requires a human.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .domain import ScreeningStatus, SupplierRecord, units_to_usdc, utcnow
from .forecast import SETTLED_STATES
from .risk import effective_limit_units, is_downgrade, is_upgrade, risk_tier

FIRST_CHECK = "first_check"


@dataclass(frozen=True)
class RescreenOutcome:
    supplier_id: str
    name: str | None
    wallet: str | None
    status: str
    tier: str
    previous_status: str | None
    previous_tier: str | None
    direction: str
    rechecked: bool
    invoices_annotated: int
    provider: str | None
    dataset: str | None
    response_hash: str | None
    checked_at: str
    detail: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "supplier_id": self.supplier_id,
            "name": self.name,
            "wallet": self.wallet,
            "status": self.status,
            "tier": self.tier,
            "previous_status": self.previous_status,
            "previous_tier": self.previous_tier,
            "direction": self.direction,
            "rechecked": self.rechecked,
            "invoices_annotated": self.invoices_annotated,
            "provider": self.provider,
            "dataset": self.dataset,
            "response_hash": self.response_hash,
            "checked_at": self.checked_at,
            "detail": self.detail,
        }


def open_invoices_by_supplier(workflow) -> dict[str, list]:
    """Suppliers with at least one invoice that has not already been paid."""
    grouped: dict[str, list] = {}
    for invoice, state in workflow.store.list_invoices():
        if state in SETTLED_STATES:
            continue
        grouped.setdefault(invoice.supplier_id, []).append(invoice)
    return grouped


def _supplier_for(workflow, supplier_id: str, invoice):
    try:
        evidence = workflow.accounting.get_invoice_evidence(invoice)
    except Exception:  # a broken connection must not be mistaken for a clean counterparty
        return None
    supplier: SupplierRecord | None = evidence.supplier
    if supplier is None:
        return None
    return supplier


def rescreen_suppliers(
    workflow,
    *,
    force: bool = False,
    now: datetime | None = None,
    source: str = "schedule",
) -> list[RescreenOutcome]:
    """Re-screen every counterparty with an open invoice, recording the result either way."""
    now = now or utcnow()
    interval = timedelta(hours=int(getattr(workflow.settings, "rescreen_interval_hours", 24)))
    outcomes: list[RescreenOutcome] = []

    for supplier_id, invoices in open_invoices_by_supplier(workflow).items():
        invoice = invoices[0]
        supplier = _supplier_for(workflow, supplier_id, invoice)
        wallet = supplier.approved_wallet if supplier else None
        previous = workflow.store.latest_screening(supplier_id)
        if previous and not force:
            checked_at = _parse(previous["checked_at"])
            if checked_at and now - checked_at < interval:
                outcomes.append(_outcome(
                    supplier_id, supplier, previous["status"], previous["tier"], previous, FIRST_CHECK,
                    rechecked=False, annotated=0, detail="Not due: the last check is inside the re-screening interval.",
                ))
                continue

        screening = workflow.screener.screen(supplier, wallet)
        tier = risk_tier(screening.status)
        workflow.store.record_screening({
            "supplier_id": supplier_id,
            "wallet": wallet,
            "status": screening.status.value,
            "tier": tier,
            "provider": screening.provider,
            "dataset": screening.dataset,
            "response_hash": screening.response_hash,
            "matches": [match.to_dict() for match in screening.matches],
            "reason": screening.reason,
            "source": source,
            "checked_at": screening.checked_at.isoformat(),
        })

        previous_status = previous["status"] if previous else None
        previous_tier = previous["tier"] if previous else None
        direction = _direction(previous_status, screening.status.value, previous_tier, tier)
        annotated = 0
        if direction in {"downgrade", "upgrade"}:
            annotated = _annotate_open_invoices(workflow, invoices, screening, previous_status, direction)

        detail = {
            "first_check": f"First recorded screening for this counterparty: {screening.status.value}.",
            "downgrade": f"Risk increased from {previous_status} to {screening.status.value}; the automatic limit falls to {units_to_usdc(effective_limit_units(workflow.settings, screening.status))} USDC.",
            "upgrade": f"Risk decreased from {previous_status} to {screening.status.value}.",
            "unchanged": f"Risk unchanged at {screening.status.value}.",
        }[direction]
        outcomes.append(_outcome(
            supplier_id, supplier, screening.status.value, tier, previous, direction,
            rechecked=True, annotated=annotated, detail=detail,
            provider=screening.provider, dataset=screening.dataset, response_hash=screening.response_hash,
            checked_at=screening.checked_at.isoformat(),
        ))
    return outcomes


def _annotate_open_invoices(workflow, invoices, screening, previous_status: str | None, direction: str) -> int:
    """Record the risk change on each affected invoice's audit chain."""
    annotated = 0
    for invoice in invoices:
        state = workflow.store.get_state(invoice.id)
        if state is None or state in SETTLED_STATES:
            continue
        workflow.store.set_state(
            invoice.id,
            state,
            "SUPPLIER_RISK_CHANGED",
            {
                "supplier_id": invoice.supplier_id,
                "previous_status": previous_status,
                "status": screening.status.value,
                "direction": direction,
                "provider": screening.provider,
                "dataset": screening.dataset,
                "response_hash": screening.response_hash,
                "reason": screening.reason,
            },
        )
        annotated += 1
    return annotated


def _direction(previous_status: str | None, status: str, previous_tier: str | None, tier: str) -> str:
    if previous_status is None:
        return FIRST_CHECK
    if is_downgrade(previous_tier, tier):
        return "downgrade"
    if is_upgrade(previous_tier, tier):
        return "upgrade"
    if previous_status != status:
        # Same tier, different label: report it, but do not claim risk moved.
        return "unchanged"
    return "unchanged"


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _outcome(
    supplier_id, supplier, status, tier, previous, direction, *, rechecked, annotated, detail,
    provider=None, dataset=None, response_hash=None, checked_at=None,
) -> RescreenOutcome:
    return RescreenOutcome(
        supplier_id=supplier_id,
        name=getattr(supplier, "name", None),
        wallet=getattr(supplier, "approved_wallet", None),
        status=status,
        tier=tier,
        previous_status=previous["status"] if previous else None,
        previous_tier=previous["tier"] if previous else None,
        direction=direction,
        rechecked=rechecked,
        invoices_annotated=annotated,
        provider=provider or (previous or {}).get("provider"),
        dataset=dataset or (previous or {}).get("dataset"),
        response_hash=response_hash or (previous or {}).get("response_hash"),
        checked_at=checked_at or (previous or {}).get("checked_at") or utcnow().isoformat(),
        detail=detail,
    )


def supplier_risk_overview(workflow) -> list[dict[str, Any]]:
    """Counterparties with their latest screening, tier, resulting limit and open invoice count."""
    grouped = open_invoices_by_supplier(workflow)
    overview: list[dict[str, Any]] = []
    for supplier_id, invoices in grouped.items():
        supplier = _supplier_for(workflow, supplier_id, invoices[0])
        latest = workflow.store.latest_screening(supplier_id)
        status = ScreeningStatus(latest["status"]) if latest else None
        overview.append({
            "supplier_id": supplier_id,
            "name": getattr(supplier, "name", None),
            "wallet": getattr(supplier, "approved_wallet", None),
            "wallet_verified": bool(getattr(supplier, "wallet_verified", False)),
            "latest_screening": latest,
            "risk_tier": latest["tier"] if latest else None,
            "automatic_limit_usdc": (
                units_to_usdc(effective_limit_units(workflow.settings, status)) if status else None
            ),
            "open_invoices": len(invoices),
            "open_amount_usdc": units_to_usdc(sum(item.amount_units for item in invoices)),
        })
    return sorted(overview, key=lambda item: str(item["supplier_id"]))


__all__ = [
    "FIRST_CHECK",
    "RescreenOutcome",
    "open_invoices_by_supplier",
    "rescreen_suppliers",
    "supplier_risk_overview",
]
