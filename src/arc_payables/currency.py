from __future__ import annotations

from decimal import Decimal

from .domain import USDC_DECIMALS

# Both the settlement and the accounting side are compared as integer units at this scale so
# that no float ever participates in an authorization decision.
UNIT_SCALE = Decimal(10) ** USDC_DECIMALS


class USDCOnlyConverter:
    """Settlement strategy for USDC-denominated invoices.

    Conversion is never implicit. An invoice in a currency without an explicitly configured
    rate returns ``None``, which the policy reports as a blocker and never treats as 1:1.
    """

    def __init__(self, invoice_currency: str = "USD", settlement_to_invoice_rate: Decimal | None = None):
        self.invoice_currency = (invoice_currency or "").upper()
        rate = Decimal(str(settlement_to_invoice_rate if settlement_to_invoice_rate is not None else 1))
        if rate <= 0:
            raise ValueError("settlement-to-invoice rate must be positive")
        self.settlement_to_invoice_rate = rate

    def settlement_amount_usdc(self, amount_units: int, currency: str) -> int | None:
        """USDC units to transfer on chain for an invoice amount."""
        if currency.upper() != "USDC":
            return None
        if amount_units <= 0:
            return None
        return amount_units

    def invoice_units_from_settlement(self, settlement_units: int, invoice_currency: str | None = None) -> int | None:
        """Expected accounting amount, in invoice-currency units, for a settled amount.

        Used to prove that the accounting payable and the authorized settlement describe the
        same money before any payment entry is written.
        """
        target = (invoice_currency or self.invoice_currency).upper()
        if not self.invoice_currency or target != self.invoice_currency:
            return None
        if settlement_units <= 0:
            return None
        scaled = Decimal(settlement_units) * self.settlement_to_invoice_rate
        if scaled != scaled.to_integral_value():
            return None
        return int(scaled)


__all__ = ["USDC_DECIMALS", "USDCOnlyConverter"]
