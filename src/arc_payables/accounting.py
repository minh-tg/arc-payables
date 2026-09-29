"""Payment-entry arithmetic for an ERPNext "Pay" entry.

Everything here follows the official ERPNext Payment Entry behaviour, so a misconfiguration
is rejected locally with an explicit message instead of failing deep inside a live submit:

* ``Paid Amount`` and ``Received Amount`` are amounts in the respective *account* currencies,
  while deduction rows and ``difference_amount`` are in the *company* currency.
* Deduction accounts must be in the company currency (ERPNext throws otherwise).
* ``received_amount`` is forced equal to ``paid_amount`` when the paid-from and party account
  currencies are the same (``Payment Entry.set_received_amount``).
* For a Pay entry, ``difference_amount == base_paid_amount - base_party_amount -
  total_deductions`` and must be zero.
* Deductions reduce what the party receives, so the network fee is added to ``paid_amount``
  and booked as a deduction. The party leg stays exactly the invoice amount.

The Arc network fee is always absorbed by us: it never reduces the supplier's payment and
never alters the invoice amount. It is recorded as its own network-fee expense.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .domain import USDC_SCALE

CENT = Decimal("0.000001")


class AccountingMappingError(RuntimeError):
    """Raised when the configured accounting mapping cannot express a correct payment."""


@dataclass(frozen=True)
class PaymentEntryAmounts:
    """Amounts for an ERPNext Payment Entry, unit-scaled exactly as ERPNext expects."""

    supplier_amount: Decimal
    """What the supplier receives, in the settlement currency: exactly the authorized amount."""

    fee_amount: Decimal
    """Arc network fee in the settlement currency, absorbed by us."""

    paid_amount: Decimal
    """Total settlement-currency outflow: supplier amount plus network fee."""

    received_amount: Decimal
    """Amount in the party account currency, following ERPNext's own rules."""

    allocated_amount: Decimal
    """Party-currency amount allocated against the Purchase Invoice."""

    total_deductions: Decimal
    """Company-currency deductions; the network fee is expensed here."""

    difference_amount: Decimal
    """ERPNext requires zero."""

    base_paid_amount: Decimal
    """Company-currency value of the outflow."""

    base_received_amount: Decimal
    """Company-currency value credited to the party."""

    unallocated_amount: Decimal
    """ERPNext would keep this as a party advance. Must be zero so the invoice settles in full."""


def _plain(value: Decimal) -> str:
    """Fixed-point string, never scientific notation."""
    return format(value.quantize(CENT).normalize(), "f")


def _decimal(value: object, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:  # pragma: no cover - defensive
        raise AccountingMappingError(f"{field} is not a number") from exc
    if not parsed.is_finite():
        raise AccountingMappingError(f"{field} must be finite")
    return parsed


def compute_payment_entry_amounts(
    amount_units: int,
    fee_units: int,
    *,
    source_currency: str,
    target_currency: str,
    company_currency: str,
    source_exchange_rate: object,
    target_exchange_rate: object,
) -> PaymentEntryAmounts:
    """Compute a balanced Payment Entry that absorbs ``fee_units`` as a separate expense.

    ``amount_units`` and ``fee_units`` are integer units at 1e-6 of the settlement currency,
    so no float participates in the arithmetic.
    """
    if amount_units <= 0:
        raise AccountingMappingError("The authorized settlement amount must be positive")
    if fee_units < 0:
        raise AccountingMappingError("The network fee cannot be negative")

    source_currency = (source_currency or "").upper()
    target_currency = (target_currency or "").upper()
    company_currency = (company_currency or "").upper()
    if not source_currency or not target_currency or not company_currency:
        raise AccountingMappingError("Source, target and company currencies are all required")

    source_rate = _decimal(source_exchange_rate, "source_exchange_rate")
    target_rate = _decimal(target_exchange_rate, "target_exchange_rate")
    if source_rate <= 0 or target_rate <= 0:
        raise AccountingMappingError("Exchange rates must be positive")
    if target_currency != company_currency:
        # Keeping the payable account in the company currency removes a second FX leg, which
        # is what makes this mapping exactly checkable before submission.
        raise AccountingMappingError(
            f"The payable account currency ({target_currency}) must equal the company currency "
            f"({company_currency}) for this mapping"
        )
    if source_currency == company_currency and source_rate != 1:
        raise AccountingMappingError(
            f"A {source_currency} settlement account in a {company_currency} company must use a source rate of 1"
        )

    supplier_amount = Decimal(amount_units) / USDC_SCALE
    fee_amount = Decimal(fee_units) / USDC_SCALE

    # Settlement-currency outflow: the supplier's exact amount plus the fee we absorb.
    paid_amount = supplier_amount + fee_amount
    if paid_amount - fee_amount != supplier_amount:  # defensive invariant
        raise AccountingMappingError("The supplier amount must never be reduced by the network fee")

    # Party currency is the company currency, so the invoice value converts with one rate.
    allocated_amount = supplier_amount * source_rate

    if source_currency == target_currency:
        # ERPNext: received_amount = paid_amount when both account currencies are equal.
        received_amount = paid_amount
    else:
        # The supplier receives exactly the authorized amount, expressed in party currency.
        received_amount = allocated_amount

    base_paid_amount = paid_amount * source_rate
    base_received_amount = received_amount * target_rate
    total_deductions = fee_amount * source_rate

    # ERPNext: difference_amount = base_paid_amount - base_party_amount - total_deductions,
    # where base_party_amount is the allocated amount plus any unallocated advance (zero here).
    difference_amount = base_paid_amount - allocated_amount - total_deductions
    if difference_amount != 0:
        raise AccountingMappingError(
            "Configured accounting mapping does not balance: the ERPNext difference amount would be "
            f"{_plain(difference_amount)} instead of 0. Check the settlement-to-invoice rate and the "
            "network-fee account currency."
        )

    # ERPNext: unallocated_amount is set when allocated < received - deductions. A non-zero
    # value here would mean the invoice is not settled in full.
    unallocated_amount = Decimal(0)
    if allocated_amount < (base_received_amount - total_deductions):
        unallocated_amount = (base_received_amount - total_deductions - allocated_amount) / target_rate
    if unallocated_amount != 0:
        raise AccountingMappingError(
            "Configured mapping would leave an unallocated party advance instead of settling the invoice"
        )

    return PaymentEntryAmounts(
        supplier_amount=supplier_amount,
        fee_amount=fee_amount,
        paid_amount=paid_amount,
        received_amount=received_amount,
        allocated_amount=allocated_amount,
        total_deductions=total_deductions,
        difference_amount=difference_amount,
        base_paid_amount=base_paid_amount,
        base_received_amount=base_received_amount,
        unallocated_amount=unallocated_amount,
    )


def payload_fields(amounts: PaymentEntryAmounts, reference_name: str, fee_account: str) -> dict:
    """Map computed amounts onto ERPNext REST field names."""
    payload: dict = {
        "paid_amount": _plain(amounts.paid_amount),
        "received_amount": _plain(amounts.received_amount),
        "references": [
            {
                "reference_doctype": "Purchase Invoice",
                "reference_name": reference_name,
                "allocated_amount": _plain(amounts.allocated_amount),
            }
        ],
    }
    if amounts.total_deductions > 0:
        payload["deductions"] = [
            {"account": fee_account, "amount": _plain(amounts.total_deductions)},
        ]
    return payload
