from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False, extra="ignore")

    app_name: str = "Arc Payables AP Agent"
    environment: str = "local"
    database_path: Path = Path("data/arc_payables.sqlite3")
    payment_provider: Literal["mock", "circle", "local"] = "mock"
    accounting_provider: Literal["mock", "frappe"] = "mock"
    policy_version: str = "arc-payables-ap-v1"
    max_invoice_usdc: Decimal = Decimal("1000")
    min_reserve_usdc: Decimal = Decimal("2000")
    max_treasury_snapshot_age_seconds: int = 90
    permit_lifetime_seconds: int = 300
    payment_due_window_days: int = 3
    discount_min_percent: Decimal = Decimal("0")
    approval_token: str | None = None
    api_key: str | None = None

    # Policy signer. `env` is the MVP backend; `kms` must be implemented behind the
    # PermitSigner interface before use. The key is read only by the payment service.
    signer_backend: Literal["env", "kms"] = "env"

    # Advisory decision layer. Optional by design: everything works with the deterministic
    # policy alone. `policy` uses no advisory layer at all, `heuristics` is the fast
    # explainable layer, and `dual_process` adds a bounded planner for judgement calls.
    # A deliberating planner can never authorize anything; the policy and the guard decide.
    decision_layer: Literal["policy", "heuristics", "dual_process"] = "heuristics"
    planner_base_url: str | None = None
    planner_api_key: str | None = Field(default=None, repr=False)
    planner_model: str | None = None
    planner_timeout_seconds: float = 20.0
    planner_max_tokens: int = 700

    # Screening. `fixture` keeps the local demo deterministic; `opensanctions` is the first
    # real provider; `unavailable` is the fail-closed failure-injection implementation.
    screening_provider: Literal["fixture", "opensanctions", "unavailable"] = "fixture"
    screening_timeout_seconds: float = 15.0
    screening_positive_match_policy: Literal["block", "review"] = "block"
    # How to treat a counterparty whose screening is unclear (inconclusive or unavailable).
    # `review` (default) requires a human, which is the fail-closed posture. `limit` instead
    # allows an unattended payment up to a reduced share of the automatic limit, which is the
    # risk-tiered behaviour a mature desk would use. A flagged counterparty always requires a
    # human either way.
    screening_medium_tier_handling: Literal["review", "limit"] = "review"
    # How often an existing counterparty is re-screened by the monitoring pass.
    rescreen_interval_hours: int = 24
    opensanctions_api_key: str | None = Field(default=None, repr=False)
    opensanctions_base_url: str = "https://api.opensanctions.org"
    opensanctions_dataset: str = "default"
    opensanctions_review_threshold: float = 0.5
    opensanctions_limit: int = 5

    # Circle Developer-Controlled Wallets (Arc Testnet only).
    circle_api_key: str | None = None
    circle_entity_secret: str | None = None
    circle_wallet_id: str | None = None
    circle_wallet_address: str | None = None
    circle_guard_address: str | None = None
    circle_rpc_url: str = "https://rpc.testnet.arc.io"
    # Circle's real API base by default; overridable so the documented integration can be
    # exercised against the local credential-free test executor.
    circle_api_base_url: str = "https://api.circle.com/v1/w3s"
    permit_signing_private_key: str | None = Field(default=None, repr=False)
    circle_poll_interval_seconds: float = 2.0
    circle_confirmation_timeout_seconds: int = 90

    # Local-key (EOA) executor: an alternative to Circle's Developer-Controlled Wallets for
    # Arc Testnet. Here the payer key is held locally, which makes it suitable for testnet
    # runs and for developing without Circle credentials. It is deliberately Arc Testnet
    # only, and the guard's on-chain budget limits apply exactly as they do for Circle.
    local_payment_private_key: str | None = Field(default=None, repr=False)
    local_payment_address: str | None = None
    local_payment_rpc_url: str = "https://rpc.testnet.arc.io"
    local_payment_guard_address: str | None = None
    local_payment_timeout_seconds: float = 20.0
    local_payment_receipt_timeout_seconds: float = 90.0
    local_payment_log_lookback_blocks: int = 20_000

    # Frappe API v1. Live writeback is disabled unless every accounting mapping value below
    # is explicitly configured and verified against the sandbox chart of accounts.
    frappe_url: str | None = None
    frappe_api_key: str | None = Field(default=None, repr=False)
    frappe_api_secret: str | None = Field(default=None, repr=False)
    frappe_timeout_seconds: float = 12.0
    frappe_supplier_wallet_field: str = "custom_usdc_wallet_address"
    frappe_supplier_wallet_verified_field: str = "custom_usdc_wallet_verified"
    frappe_invoice_payee_field: str | None = None
    frappe_company: str | None = None
    frappe_paid_from_account: str | None = None
    frappe_paid_to_account: str | None = None
    frappe_mode_of_payment: str | None = None
    frappe_settlement_currency: str | None = None
    frappe_company_currency: str | None = None
    frappe_invoice_currency: str | None = None
    frappe_source_exchange_rate: Decimal | None = None
    frappe_target_exchange_rate: Decimal | None = None
    frappe_fee_account: str | None = None
    frappe_fee_currency: str | None = None

    @property
    def settlement_currency(self) -> str:
        """Currency transferred on chain. Only USDC is implemented in this MVP."""
        return "USDC"

    @property
    def settlement_to_invoice_rate(self) -> Decimal:
        """Company-currency units per USDC, used to compare the ERP payable with the settlement."""
        return self.frappe_source_exchange_rate or Decimal(1)

    @property
    def max_invoice_units(self) -> int:
        return int(self.max_invoice_usdc * Decimal(1_000_000))

    @property
    def min_reserve_units(self) -> int:
        return int(self.min_reserve_usdc * Decimal(1_000_000))

    @property
    def circle_ready(self) -> bool:
        required = (
            self.circle_api_key,
            self.circle_entity_secret,
            self.circle_wallet_id,
            self.circle_wallet_address,
            self.circle_guard_address,
            self.permit_signing_private_key,
        )
        return all(value and value.strip() for value in required)

    @property
    def planner_configured(self) -> bool:
        """True when a deliberating planner could actually be called."""
        return bool(self.decision_layer == "dual_process" and self.planner_base_url and self.planner_model)

    @property
    def local_payment_ready(self) -> bool:
        required = (
            self.local_payment_private_key,
            self.local_payment_guard_address,
            self.local_payment_rpc_url,
        )
        return all(value and str(value).strip() for value in required)

    @property
    def frappe_accounting_ready(self) -> bool:
        required = (
            self.frappe_url,
            self.frappe_api_key,
            self.frappe_api_secret,
            self.frappe_company,
            self.frappe_paid_from_account,
            self.frappe_paid_to_account,
            self.frappe_mode_of_payment,
            self.frappe_settlement_currency,
            self.frappe_company_currency,
            self.frappe_invoice_currency,
            self.frappe_source_exchange_rate,
            self.frappe_target_exchange_rate,
            self.frappe_fee_account,
            self.frappe_fee_currency,
        )
        if not all(value is not None and value != "" for value in required):
            return False
        rates = (self.frappe_source_exchange_rate, self.frappe_target_exchange_rate)
        if not all(rate is not None and rate > 0 for rate in rates):
            return False
        # The payable account and the fee account must be in the company currency, otherwise
        # ERPNext rejects the deduction row or the entry cannot balance.
        return (
            (self.frappe_target_currency or self.frappe_company_currency or "").upper()
            == (self.frappe_company_currency or "").upper()
            and (self.frappe_invoice_currency or "").upper() == (self.frappe_company_currency or "").upper()
            and (self.frappe_fee_currency or "").upper() == (self.frappe_company_currency or "").upper()
        )

    @property
    def frappe_target_currency(self) -> str | None:
        """Payable account currency; pinned to the company currency by design."""
        return self.frappe_company_currency


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
