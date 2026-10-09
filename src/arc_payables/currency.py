from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from .domain import USDC_DECIMALS

# Both the settlement and the accounting side are compared as integer units at this scale so
# that no float ever participates in an authorization decision.
UNIT_SCALE = Decimal(10) ** USDC_DECIMALS


@dataclass(frozen=True)
class CurrencyRate:
    """Explicit conversion parameters between an invoice currency and settlement USDC."""

    currency: str
    rate_to_usdc: Decimal
    """USDC units per 1 unit of foreign currency. Decimal('1.08') means 1 EUR = 1.08 USDC."""

    decimals: int = USDC_DECIMALS
    """Decimal precision of the currency (default 6 matching USDC micro-units)."""

    max_slippage_bps: int = 50
    """Maximum permitted rate slippage in basis points (1 bp = 0.01%)."""

    source: str = "configured"
    updated_at: datetime | None = None


@dataclass(frozen=True)
class OracleRateQuote:
    """Attested currency conversion quote from an external oracle feed."""

    currency: str
    rate_to_usdc: Decimal
    timestamp: datetime
    source: str = "oracle"
    signature: str | None = None


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


class MultiCurrencyConverter:
    """Multi-currency settlement converter with explicit exchange rates.

    Supports non-USDC invoices (e.g. EUR, GBP, USD) by converting to USDC on-chain settlement units
    using explicit, non-implicit exchange rates and deterministic integer unit arithmetic.
    """

    def __init__(
        self,
        invoice_currency: str = "USD",
        rates: dict[str, Decimal | CurrencyRate] | None = None,
    ):
        self.invoice_currency = (invoice_currency or "").upper()
        self._rates: dict[str, CurrencyRate] = {
            "USDC": CurrencyRate(
                currency="USDC",
                rate_to_usdc=Decimal(1),
                decimals=USDC_DECIMALS,
                source="native",
            )
        }
        if rates:
            for curr, rate in rates.items():
                self.register_rate(curr, rate)

    @property
    def settlement_to_invoice_rate(self) -> Decimal:
        """Invoice currency units per 1 USDC for protocol compatibility."""
        rate = self._rates.get(self.invoice_currency)
        if rate and rate.rate_to_usdc > 0:
            return (Decimal(1) / rate.rate_to_usdc).quantize(Decimal("0.00000001"))
        return Decimal(1)

    def register_rate(self, currency: str, rate: Decimal | CurrencyRate) -> None:
        curr = currency.upper()
        if isinstance(rate, CurrencyRate):
            if rate.rate_to_usdc <= 0:
                raise ValueError(f"Exchange rate for {curr} must be positive")
            self._rates[curr] = rate
        else:
            dec_rate = Decimal(str(rate))
            if dec_rate <= 0:
                raise ValueError(f"Exchange rate for {curr} must be positive")
            self._rates[curr] = CurrencyRate(
                currency=curr,
                rate_to_usdc=dec_rate,
                decimals=USDC_DECIMALS,
                source="configured",
            )

    def get_rate(self, currency: str) -> CurrencyRate | None:
        return self._rates.get(currency.upper())

    def supported_currencies(self) -> list[str]:
        return sorted(self._rates.keys())

    def settlement_amount_usdc(self, amount_units: int, currency: str) -> int | None:
        """USDC units to transfer on chain for an invoice amount in given currency."""
        if amount_units <= 0:
            return None
        curr = currency.upper()
        rate_info = self._rates.get(curr)
        if rate_info is None:
            return None
        foreign_scale = Decimal(10) ** rate_info.decimals
        usdc_units = (Decimal(amount_units) / foreign_scale) * rate_info.rate_to_usdc * UNIT_SCALE
        rounded = int(usdc_units.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return rounded if rounded > 0 else None

    def invoice_units_from_settlement(
        self, settlement_units: int, invoice_currency: str | None = None
    ) -> int | None:
        """Expected accounting amount, in invoice-currency units, for settled USDC amount."""
        if settlement_units <= 0:
            return None
        target = (invoice_currency or self.invoice_currency).upper()
        rate_info = self._rates.get(target)
        if rate_info is None:
            return None
        foreign_scale = Decimal(10) ** rate_info.decimals
        invoice_units = ((Decimal(settlement_units) / UNIT_SCALE) / rate_info.rate_to_usdc) * foreign_scale
        rounded = int(invoice_units.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return rounded if rounded > 0 else None


class OracleCurrencyConverter(MultiCurrencyConverter):
    """Multi-currency converter backed by an attested oracle rate feed.

    Enforces freshness deadlines (fail-closed if stale) and slippage tolerance boundaries
    against configured baseline rates.
    """

    def __init__(
        self,
        invoice_currency: str = "USD",
        base_rates: dict[str, Decimal | CurrencyRate] | None = None,
        max_age_seconds: int = 3600,
        allowed_slippage_bps: int = 100,
    ):
        super().__init__(invoice_currency=invoice_currency, rates=base_rates)
        self.max_age_seconds = max_age_seconds
        self.allowed_slippage_bps = allowed_slippage_bps
        self._reference_rates: dict[str, Decimal] = {
            curr: rate.rate_to_usdc for curr, rate in self._rates.items()
        }

    def update_quote(self, quote: OracleRateQuote, now: datetime | None = None) -> None:
        """Ingest a fresh oracle quote after freshness and slippage validation."""
        curr = quote.currency.upper()
        current_time = now or datetime.now(timezone.utc)
        quote_time = quote.timestamp if quote.timestamp.tzinfo else quote.timestamp.replace(tzinfo=timezone.utc)

        age = (current_time - quote_time).total_seconds()
        if age > self.max_age_seconds:
            raise ValueError(f"Oracle quote for {curr} is stale ({age:.1f}s > {self.max_age_seconds}s)")
        if age < -60:
            raise ValueError(f"Oracle quote for {curr} is timestamped in the future")

        ref = self._reference_rates.get(curr)
        if ref is not None and ref > 0:
            slippage_bps = abs(quote.rate_to_usdc - ref) / ref * Decimal(10000)
            if slippage_bps > self.allowed_slippage_bps:
                raise ValueError(
                    f"Oracle rate {quote.rate_to_usdc} for {curr} exceeds slippage tolerance "
                    f"({slippage_bps:.1f} bps > {self.allowed_slippage_bps} bps relative to reference {ref})"
                )

        self._rates[curr] = CurrencyRate(
            currency=curr,
            rate_to_usdc=quote.rate_to_usdc,
            decimals=USDC_DECIMALS,
            max_slippage_bps=self.allowed_slippage_bps,
            source=quote.source,
            updated_at=quote_time,
        )

    def is_fresh(self, currency: str, now: datetime | None = None) -> bool:
        """Verify whether the quote for a currency is currently fresh."""
        curr = currency.upper()
        if curr == "USDC":
            return True
        rate = self._rates.get(curr)
        if rate is None:
            return False
        if rate.updated_at is None:
            # Baseline configured rate without explicit oracle quote
            return True
        current_time = now or datetime.now(timezone.utc)
        age = (current_time - rate.updated_at).total_seconds()
        return 0 <= age <= self.max_age_seconds

    def settlement_amount_usdc(
        self, amount_units: int, currency: str, as_of: datetime | None = None
    ) -> int | None:
        """Compute USDC settlement units, failing closed if the oracle rate is stale."""
        if not self.is_fresh(currency, now=as_of):
            return None
        return super().settlement_amount_usdc(amount_units, currency)


__all__ = [
    "CurrencyRate",
    "MultiCurrencyConverter",
    "OracleCurrencyConverter",
    "OracleRateQuote",
    "UNIT_SCALE",
    "USDC_DECIMALS",
    "USDCOnlyConverter",
]
