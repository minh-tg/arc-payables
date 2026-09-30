"""Build the shared application workflow without constructing the HTTP app.

The API and worker must use the same adapters and policy, but a worker process has no reason to
import FastAPI, mount static files, or create a second application object. This module is the
composition root shared by both entry points.
"""

from __future__ import annotations

from typing import Any

from .circle_adapter import CircleDeveloperControlledWalletProvider
from .currency import USDCOnlyConverter
from .domain import ScreeningStatus
from .frappe_adapter import FrappeAccountingConnector
from .local_payment import LocalKeyPaymentProvider
from .mock_adapters import MockAccountingConnector, MockPaymentProvider
from .policy import DeterministicPolicy
from .screening import build_screening_provider
from .security import EIP712PermitSigner, build_permit_signer
from .service import APWorkflow
from .settings import Settings
from .store import SQLiteEvidenceStore


class DisabledPaymentProvider:
    """A fail-closed adapter used when a real payment provider is incomplete."""

    wallet_address = None
    guard_address = None

    def get_balance(self):
        raise RuntimeError("Circle payment configuration is incomplete")

    def screen_address(self, address):
        return ScreeningStatus.UNAVAILABLE

    def inspect_payment(self, payment):
        raise RuntimeError("Circle payment configuration is incomplete")

    def submit_authorized(self, payment, on_transaction=None):
        raise RuntimeError("Circle payment configuration is incomplete")


def build_workflow(
    settings: Settings,
    *,
    store: SQLiteEvidenceStore | None = None,
    accounting: Any | None = None,
    payment_provider: Any | None = None,
    signer: Any | None = None,
) -> APWorkflow:
    """Construct the durable workflow and its providers for either process entry point."""
    store = store or SQLiteEvidenceStore(settings.database_path)
    store.initialize()

    if accounting is None:
        accounting = (
            FrappeAccountingConnector(settings)
            if settings.accounting_provider == "frappe"
            else MockAccountingConnector(
                store,
                invoice_currency=settings.frappe_invoice_currency or "USD",
                settlement_currency=settings.settlement_currency,
                settlement_to_invoice_rate=settings.settlement_to_invoice_rate,
            )
        )
    if payment_provider is None:
        if settings.payment_provider == "circle":
            if settings.circle_ready:
                signer = signer or build_permit_signer(settings)
                payment_provider = CircleDeveloperControlledWalletProvider(settings, signer)
            else:
                payment_provider = DisabledPaymentProvider()
        elif settings.payment_provider == "local":
            if settings.local_payment_ready:
                signer = signer or build_permit_signer(settings)
                payment_provider = LocalKeyPaymentProvider(settings, signer)
            else:
                payment_provider = DisabledPaymentProvider()
        else:
            payment_provider = MockPaymentProvider(store)
    if signer is None:
        signer = getattr(payment_provider, "signer", None)
    if signer is None:
        # A throwaway signer keeps the non-payment demo constructible; it cannot authorize a payment.
        signer = EIP712PermitSigner("0x" + "01".zfill(64))

    store.set_audit_signer(signer)
    policy = DeterministicPolicy(settings, USDCOnlyConverter())
    return APWorkflow(
        store,
        accounting,
        payment_provider,
        signer,
        policy,
        settings,
        screener=build_screening_provider(settings, store),
    )
