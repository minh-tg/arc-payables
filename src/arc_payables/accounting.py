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
* Any gap between what left our account and what the party received is booked by ERPNext as an
  exchange gain or loss (``set_exchange_gain_loss``), and a deduction row is subtracted from what
  the party receives (``paid_amount -= sum(d.amount for d in deductions)``).

The Arc network fee is therefore never part of the Payment Entry. A fee we absorb is not a
deduction from the supplier and not an exchange difference: it is our own cost, recorded as a
separate journal entry (see ``compute_fee_expense``). The Payment Entry is exactly the
authorized supplier amount, so it balances on its own terms and the supplier's amount is exact.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from .domain import USDC_SCALE

CENT = Decimal("0.000001")


class AccountingMappingError(RuntimeError):
    """Raised when the configured accounting mapping cannot express a correct payment."""


@dataclass(frozen=True)
class PaymentEntryAmounts:
    """Amounts for an ERPNext Payment Entry, unit-scaled exactly as ERPNext expects."""

    supplier_amount: Decimal
    """What the supplier receives, in the settlement currency: exactly the authorized amount."""

    paid_amount: Decimal
    """Settlement-currency outflow. Exactly the supplier amount; the fee is booked separately."""

    received_amount: Decimal
    """Amount in the party account currency, following ERPNext's own rules."""

    allocated_amount: Decimal
    """Party-currency amount allocated against the Purchase Invoice."""

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


def _number(value: Decimal) -> float:
    """A JSON number for the ERPNext REST API.

    `float` is Frappe's own currency representation, so this is where exactness is handed over
    rather than lost: every check that matters has already run on the Decimal value.
    """
    return float(value.quantize(CENT).normalize())


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
    *,
    source_currency: str,
    target_currency: str,
    company_currency: str,
    source_exchange_rate: object,
    target_exchange_rate: object,
) -> PaymentEntryAmounts:
    """Compute a balanced Payment Entry for the supplier's exact amount.

    ``amount_units`` is an integer count of 1e-6 settlement-currency units, so no float
    participates in the arithmetic.
    """
    if amount_units <= 0:
        raise AccountingMappingError("The authorized settlement amount must be positive")

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
    if target_rate != 1:
        # ERPNext books base_paid_amount - base_received_amount as an exchange gain or loss, so any
        # rate on a payable account that is already in the company currency manufactures an FX row.
        raise AccountingMappingError(
            f"A {target_currency} payable account in a {company_currency} company must use a target rate of 1, "
            "otherwise ERPNext books the difference as an exchange gain or loss"
        )

    supplier_amount = Decimal(amount_units) / USDC_SCALE

    # The settlement-currency outflow is the supplier's exact amount. The network fee is our own
    # cost and is booked as a separate journal entry, so it never appears here.
    paid_amount = supplier_amount

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

    # ERPNext: difference_amount = base_paid_amount - base_party_amount - total_deductions,
    # where base_party_amount is the allocated amount plus any unallocated advance (zero here).
    difference_amount = base_paid_amount - allocated_amount
    if difference_amount != 0:
        raise AccountingMappingError(
            "Configured accounting mapping does not balance: the ERPNext difference amount would be "
            f"{_plain(difference_amount)} instead of 0. Check the settlement-to-invoice rate and the "
            "network-fee account currency."
        )

    # ERPNext: unallocated_amount is set when allocated < received - deductions. A non-zero
    # value here would mean the invoice is not settled in full.
    unallocated_amount = Decimal(0)
    if allocated_amount < base_received_amount:
        unallocated_amount = (base_received_amount - allocated_amount) / target_rate
    if unallocated_amount != 0:
        raise AccountingMappingError(
            "Configured mapping would leave an unallocated party advance instead of settling the invoice"
        )

    return PaymentEntryAmounts(
        supplier_amount=supplier_amount,
        paid_amount=paid_amount,
        received_amount=received_amount,
        allocated_amount=allocated_amount,
        difference_amount=difference_amount,
        base_paid_amount=base_paid_amount,
        base_received_amount=base_received_amount,
        unallocated_amount=unallocated_amount,
    )


def payload_fields(amounts: PaymentEntryAmounts, reference_name: str) -> dict:
    """Map computed amounts onto ERPNext REST field names."""
    # Every amount is sent as a number. Fixed-point strings looked safer, but ERPNext runs bare
    # arithmetic over these fields before any `flt()`: `sum(d.allocated_amount for d in references)`
    # and `abs(paid_amount)` both raise TypeError on a string, which the REST API reports as a 500
    # and a client can only treat as an uncertain write. Frappe models currency as a float, so the
    # boundary follows Frappe; the exact arithmetic stays here, in compute_payment_entry_amounts,
    # where it is checked before anything is sent.
    # No deduction rows: ERPNext subtracts a deduction from what the party receives, and the only
    # thing we ever want to subtract is nothing.
    payload: dict = {
        "paid_amount": _number(amounts.paid_amount),
        "received_amount": _number(amounts.received_amount),
        "references": [
            {
                "reference_doctype": "Purchase Invoice",
                "reference_name": reference_name,
                "allocated_amount": _number(amounts.allocated_amount),
            }
        ],
    }
    return payload


@dataclass(frozen=True)
class FeeExpenseAmounts:
    """The Arc network fee as our own expense, in the company currency."""

    measured_amount: Decimal
    """Fee as measured on chain, in the company currency. May be below the smallest bookable unit."""

    booked_amount: Decimal
    """Fee actually booked: ``measured_amount`` rounded up to a representable amount."""

    remark: str
    """What an accountant needs to reconcile the booked figure against the transaction."""


def bookable_fee_amount(fee_units: int, *, smallest_unit: Decimal) -> Decimal:
    """Round a measured fee up to an amount the company currency can represent.

    A fee below the currency's smallest unit cannot be booked as written. Rounding up keeps the
    entry balanced, never understates our own cost, and leaves the supplier's amount untouched.
    """
    fee = Decimal(fee_units) / USDC_SCALE
    if smallest_unit <= 0:
        return fee
    return (fee / smallest_unit).to_integral_value(rounding=ROUND_CEILING) * smallest_unit


def compute_fee_expense(
    fee_units: int,
    *,
    company_currency: str,
    source_currency: str,
    smallest_unit: Decimal,
    tx_hash: str,
    supplier_amount_units: int,
) -> FeeExpenseAmounts:
    """Compute the separate expense entry for the network fee we absorbed."""
    if fee_units <= 0:
        raise AccountingMappingError("A fee expense needs a positive measured fee")
    company_currency = (company_currency or "").upper()
    source_currency = (source_currency or "").upper()
    if not company_currency:
        raise AccountingMappingError("The company currency is required for the fee expense")

    measured = Decimal(fee_units) / USDC_SCALE
    booked = bookable_fee_amount(fee_units, smallest_unit=smallest_unit)
    supplier_amount = Decimal(supplier_amount_units) / USDC_SCALE
    if booked < measured:  # defensive invariant
        raise AccountingMappingError("The booked network fee must never be less than the measured fee")

    if booked == measured:
        rounding = "The measured fee is booked as measured."
    else:
        rounding = (
            f"Booked as {_plain(booked)} {company_currency}, rounded up to the smallest unit "
            f"{company_currency} can represent."
        )
    remark = (
        f"Arc network fee for transaction {tx_hash}: measured {_plain(measured)} {source_currency} on chain. "
        f"{rounding} The supplier received exactly {_plain(supplier_amount)} {source_currency}; "
        "this fee is absorbed by us and is not deducted from the supplier."
    )
    return FeeExpenseAmounts(measured_amount=measured, booked_amount=booked, remark=remark)


def journal_entry_fields(
    amounts: FeeExpenseAmounts,
    *,
    company: str,
    fee_account: str,
    settlement_account: str,
    cost_center: str,
    reference: str,
    posting_date: str,
    settlement_exchange_rate: str,
    multi_currency: bool,
) -> dict:
    """Map the fee expense onto an ERPNext Journal Entry: debit our fee account, credit the wallet.

    ``cheque_no`` is the field ERPNext labels "Reference Number", and it carries the Arc transaction
    reference so a retry finds the entry it already wrote instead of booking the fee twice.
    """
    amount = _number(amounts.booked_amount)
    payload = {
        "doctype": "Journal Entry",
        "voucher_type": "Journal Entry",
        "company": company,
        "posting_date": posting_date,
        "cheque_no": reference,
        # Frappe requires a reference date whenever a reference number is given.
        "cheque_date": posting_date,
        "user_remark": amounts.remark,
        "accounts": [
            {
                "account": fee_account,
                "cost_center": cost_center,
                "debit_in_account_currency": amount,
            },
            {
                "account": settlement_account,
                "credit_in_account_currency": amount,
                # The wallet is not held in the company currency, and Frappe requires the rate on
                # such a line rather than looking one up.
                "exchange_rate": settlement_exchange_rate,
            },
        ],
    }
    # A journal entry whose accounts span more than one currency has to say so; ERPNext otherwise
    # refuses it with "Please check Multi Currency option to allow accounts with other currency".
    if multi_currency:
        payload["multi_currency"] = 1
    return payload
