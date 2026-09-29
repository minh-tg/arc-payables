"""Risk tiers: a counterparty's screening result scales its automatic limit.

Screening is usually binary and point-in-time: block or allow, checked once. That is the wrong
shape for a limit. A counterparty whose risk profile is unclear should not be treated as either
clean or forbidden; it should be allowed less without a human, which is what actually happens in
a finance department.

So the tier only ever changes the *automatic* limit. It never grants authority: a flagged or
unavailable screening still has its own policy check requiring human review, so a reduced limit
can never turn a review case into an automatic payment.
"""

from __future__ import annotations

from decimal import Decimal

from .domain import USDC_SCALE

#: Screening status to risk tier. `UNAVAILABLE` is deliberately not treated as clean.
TIER_BY_STATUS = {
    "CLEAR": "low",
    "INCONCLUSIVE": "medium",
    "UNAVAILABLE": "medium",
    "FLAGGED": "high",
}

#: Fraction of the configured automatic limit a tier may spend unattended.
TIER_LIMIT_FACTORS = {
    "low": Decimal("1"),
    "medium": Decimal("0.25"),
    "high": Decimal("0"),
}

TIER_ORDER = {"low": 0, "medium": 1, "high": 2}


def risk_tier(status) -> str:
    """Tier for a screening status, treating an unknown status as the most cautious tier."""
    value = getattr(status, "value", status)
    return TIER_BY_STATUS.get(str(value).upper(), "high")


def effective_limit_units(settings, status) -> int:
    """The automatic limit in USDC units for this counterparty's risk tier."""
    factor = TIER_LIMIT_FACTORS[risk_tier(status)]
    return int(settings.max_invoice_units * factor)


def is_downgrade(previous_tier: str | None, tier: str) -> bool:
    """True when risk increased, so a caller can record or alert rather than stay quiet."""
    if previous_tier is None:
        return False
    return TIER_ORDER.get(tier, 2) > TIER_ORDER.get(previous_tier, 2)


def is_upgrade(previous_tier: str | None, tier: str) -> bool:
    if previous_tier is None:
        return False
    return TIER_ORDER.get(tier, 2) < TIER_ORDER.get(previous_tier, 2)


def tier_limit_usdc(settings, status) -> str:
    from .domain import units_to_usdc

    return units_to_usdc(effective_limit_units(settings, status))


__all__ = [
    "TIER_BY_STATUS",
    "TIER_LIMIT_FACTORS",
    "TIER_ORDER",
    "USDC_SCALE",
    "effective_limit_units",
    "is_downgrade",
    "is_upgrade",
    "risk_tier",
    "tier_limit_usdc",
]
