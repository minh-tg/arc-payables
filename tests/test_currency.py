from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
import pytest

from arc_payables.currency import (
    CurrencyRate,
    MultiCurrencyConverter,
    OracleCurrencyConverter,
    OracleRateQuote,
    USDCOnlyConverter,
)
from arc_payables.domain import DecisionAction, InvoiceLine, InvoiceRecord
from arc_payables.policy import DeterministicPolicy
from arc_payables.settings import Settings


def test_usdc_only_converter_basic():
    converter = USDCOnlyConverter(invoice_currency="USD", settlement_to_invoice_rate=Decimal(1))
    assert converter.settlement_amount_usdc(1_000_000, "USDC") == 1_000_000
    assert converter.settlement_amount_usdc(1_000_000, "EUR") is None
    assert converter.settlement_amount_usdc(1_000_000, "USD") is None
    assert converter.settlement_amount_usdc(0, "USDC") is None
    assert converter.settlement_amount_usdc(-500, "USDC") is None

    assert converter.invoice_units_from_settlement(1_000_000, "USD") == 1_000_000
    assert converter.invoice_units_from_settlement(1_000_000, "EUR") is None


def test_usdc_only_converter_invalid_rate():
    with pytest.raises(ValueError, match="positive"):
        USDCOnlyConverter(settlement_to_invoice_rate=Decimal(0))
    with pytest.raises(ValueError, match="positive"):
        USDCOnlyConverter(settlement_to_invoice_rate=Decimal("-1.5"))


def test_multi_currency_converter_explicit_rates():
    converter = MultiCurrencyConverter(
        invoice_currency="USD",
        rates={
            "USD": Decimal("1.00"),
            "EUR": Decimal("1.08"),
            "GBP": Decimal("1.25"),
        },
    )

    assert "USDC" in converter.supported_currencies()
    assert "EUR" in converter.supported_currencies()
    assert "GBP" in converter.supported_currencies()
    assert "USD" in converter.supported_currencies()

    # 100 EUR @ 1.08 = 108 USDC
    assert converter.settlement_amount_usdc(100_000_000, "EUR") == 108_000_000
    # 200 GBP @ 1.25 = 250 USDC
    assert converter.settlement_amount_usdc(200_000_000, "GBP") == 250_000_000
    # 50 USD @ 1.00 = 50 USDC
    assert converter.settlement_amount_usdc(50_000_000, "USD") == 50_000_000
    # Native USDC
    assert converter.settlement_amount_usdc(75_000_000, "USDC") == 75_000_000

    # Inverse conversions back to invoice units
    assert converter.invoice_units_from_settlement(108_000_000, "EUR") == 100_000_000
    assert converter.invoice_units_from_settlement(250_000_000, "GBP") == 200_000_000
    assert converter.invoice_units_from_settlement(50_000_000, "USD") == 50_000_000
    assert converter.invoice_units_from_settlement(75_000_000, "USDC") == 75_000_000


def test_multi_currency_unregistered_currency_rejected():
    converter = MultiCurrencyConverter(rates={"EUR": Decimal("1.08")})
    # JPY is not registered - must never implicitly convert or assume 1:1
    assert converter.settlement_amount_usdc(100_000_000, "JPY") is None
    assert converter.invoice_units_from_settlement(100_000_000, "JPY") is None


def test_multi_currency_invalid_rate_rejected():
    converter = MultiCurrencyConverter()
    with pytest.raises(ValueError, match="positive"):
        converter.register_rate("EUR", Decimal("0"))
    with pytest.raises(ValueError, match="positive"):
        converter.register_rate("EUR", Decimal("-1.2"))


def test_oracle_currency_converter_freshness_and_staleness():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    oracle = OracleCurrencyConverter(
        base_rates={"EUR": Decimal("1.08")},
        max_age_seconds=300,  # 5 minutes
        allowed_slippage_bps=100,  # 1%
    )

    # Initial configured baseline is valid
    assert oracle.is_fresh("EUR", now=now)
    assert oracle.settlement_amount_usdc(100_000_000, "EUR", as_of=now) == 108_000_000

    # Ingest a fresh quote 1 minute ago
    fresh_quote = OracleRateQuote(
        currency="EUR",
        rate_to_usdc=Decimal("1.082"),
        timestamp=now - timedelta(seconds=60),
        source="pyth_fx",
    )
    oracle.update_quote(fresh_quote, now=now)
    assert oracle.is_fresh("EUR", now=now)
    # 100 EUR * 1.082 = 108.20 USDC = 108_200_000 units
    assert oracle.settlement_amount_usdc(100_000_000, "EUR", as_of=now) == 108_200_000

    # 10 minutes later, the quote is stale -> fail closed!
    later = now + timedelta(minutes=10)
    assert not oracle.is_fresh("EUR", now=later)
    assert oracle.settlement_amount_usdc(100_000_000, "EUR", as_of=later) is None


def test_oracle_currency_converter_slippage_protection():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    oracle = OracleCurrencyConverter(
        base_rates={"EUR": Decimal("1.00")},
        max_age_seconds=600,
        allowed_slippage_bps=50,  # 0.5% max allowable movement
    )

    # 0.2% movement (within 50 bps) is accepted
    acceptable_quote = OracleRateQuote(
        currency="EUR",
        rate_to_usdc=Decimal("1.002"),
        timestamp=now,
        source="oracle",
    )
    oracle.update_quote(acceptable_quote, now=now)
    assert oracle.get_rate("EUR").rate_to_usdc == Decimal("1.002")

    # 2.0% movement (200 bps > 50 bps) must be rejected
    wild_quote = OracleRateQuote(
        currency="EUR",
        rate_to_usdc=Decimal("1.020"),
        timestamp=now,
        source="oracle",
    )
    with pytest.raises(ValueError, match="slippage tolerance"):
        oracle.update_quote(wild_quote, now=now)


def test_oracle_future_quote_rejected():
    now = datetime(2026, 10, 9, 12, 0, 0, tzinfo=timezone.utc)
    oracle = OracleCurrencyConverter(base_rates={"EUR": Decimal("1.08")})
    future_quote = OracleRateQuote(
        currency="EUR",
        rate_to_usdc=Decimal("1.08"),
        timestamp=now + timedelta(hours=1),
        source="oracle",
    )
    with pytest.raises(ValueError, match="future"):
        oracle.update_quote(future_quote, now=now)


def test_policy_evaluates_multi_currency_invoice(runtime):
    settings = runtime["settings"]
    converter = MultiCurrencyConverter(
        invoice_currency="EUR",
        rates={"EUR": Decimal("1.08")},
    )
    policy = DeterministicPolicy(settings, converter)

    today = datetime.now(timezone.utc).date()
    invoice = InvoiceRecord(
        id="inv-eur-001",
        supplier_id="sup-acme-001",
        invoice_number="INV-EUR-001",
        invoice_date=today,
        due_date=today,
        amount_units=100_000_000,  # 100.000000 EUR
        currency="EUR",
        invoice_payee_address="0x2222222222222222222222222222222222222222",
        lines=(
            InvoiceLine(
                item_code="INDUSTRIAL-FILTER",
                quantity="10",
                amount_units=100_000_000,
            ),
        ),
    )

    legit = runtime["store"].get_invoice(runtime["legitimate_id"])
    accounting = runtime["accounting"].get_invoice_evidence(legit)
    treasury = runtime["payment"].get_balance()
    screening = runtime["workflow"].screener.screen(accounting.supplier, invoice.invoice_payee_address)
    from arc_payables.ports import AgentRecommendation
    rec = AgentRecommendation(action="PAY_NOW", reason="Testing multi-currency policy", material_claims=())

    decision = policy.evaluate(invoice, accounting, treasury, screening, rec, now=today)
    currency_check = next(c for c in decision.checks if c.code == "settlement_currency")
    assert currency_check.passed
    assert "EUR" in currency_check.detail
    assert "108 USDC" in currency_check.detail
