"""The live-payment preflight: it must refuse before anything is signed.

Run against a local replica of Arc Testnet (real chain id, real guard bytecode, real USDC
interface), so the same checks that will gate a real payment are exercised for real.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from arc_payables.currency import USDCOnlyConverter
from arc_payables.domain import USDC_SCALE
from arc_payables.fake_circle import DEFAULT_POLICY_KEY, AnvilChain
from arc_payables.live_run import preflight
from arc_payables.local_payment import LocalKeyPaymentProvider
from arc_payables.mock_adapters import MockAccountingConnector
from arc_payables.policy import DeterministicPolicy
from arc_payables.seed import seed_demo
from arc_payables.security import EIP712PermitSigner
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore
from arc_payables.verify_arc import RpcClient, provider_view

INVOICE_USDC = 250


@pytest.fixture(scope="module")
def chain():
    with AnvilChain() as running:
        running.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        running.fund_native(running.wallet.address, 10**18)
        running.mint(running.wallet.address, 5_000 * USDC_SCALE)
        yield running


def _workspace(tmp_path: Path, chain, *, reserve: Decimal = Decimal("0")):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "live.sqlite3",
        payment_provider="local",
        accounting_provider="mock",
        min_reserve_usdc=reserve,
        local_payment_private_key=chain.wallet.key.hex(),
        local_payment_guard_address=chain.guard_address,
        local_payment_rpc_url=chain.url,
        permit_signing_private_key=DEFAULT_POLICY_KEY,
        local_payment_receipt_timeout_seconds=30,
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _suspicious = seed_demo(store)
    provider = LocalKeyPaymentProvider(settings, EIP712PermitSigner(DEFAULT_POLICY_KEY))
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        provider.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return settings, store, provider, workflow, legitimate_id


def _chain_with_caps(**caps) -> AnvilChain:
    chain = AnvilChain()
    chain.start()
    chain.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address, **caps)
    chain.fund_native(chain.wallet.address, 10**18)
    chain.mint(chain.wallet.address, 5_000 * USDC_SCALE)
    return chain


def test_preflight_passes_on_a_properly_configured_deployment(tmp_path, chain):
    settings, _store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        result = preflight(workflow, settings, invoice_id, rpc=RpcClient(chain.url))
        assert result.ok is True, result.findings
        assert result.recipient
        assert result.amount_usdc == "250"
        assert any("verified Supplier wallet" in line for line in result.findings)
        assert any("above the reserve floor" in line for line in result.findings)
        assert any("guard budgets" in line for line in result.findings)
    finally:
        provider.close()


def test_preflight_refuses_an_unverified_supplier_wallet(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        fixture = store.get_supplier_fixture("SUP-ACME-001")
        store.seed_fixture("supplier", "SUP-ACME-001", {**fixture, "wallet_verified": False})
        result = preflight(workflow, settings, invoice_id, rpc=RpcClient(chain.url))
        assert result.ok is False
        assert any("not verified" in line for line in result.findings)
    finally:
        provider.close()


def test_preflight_refuses_a_reserve_breach(tmp_path, chain):
    """A 4,900 USDC floor against a 5,000 balance and a 250 invoice must stop the payment."""
    settings, _store, provider, workflow, invoice_id = _workspace(tmp_path, chain, reserve=Decimal("4900"))
    try:
        result = preflight(workflow, settings, invoice_id, rpc=RpcClient(chain.url))
        assert result.ok is False
        assert any("breach the configured treasury reserve" in line for line in result.findings)
    finally:
        provider.close()


def test_preflight_refuses_a_payment_the_guard_would_reject(tmp_path):
    """A 100 USDC per-payment cap must stop a 250 USDC payment before it is signed."""
    bounded = _chain_with_caps(
        per_payment_cap=100 * USDC_SCALE,
        epoch_cap=1_000 * USDC_SCALE,
        recipient_epoch_cap=500 * USDC_SCALE,
    )
    try:
        settings, _store, provider, workflow, invoice_id = _workspace(tmp_path, bounded)
        try:
            result = preflight(workflow, settings, invoice_id, rpc=RpcClient(bounded.url))
            assert result.ok is False
            assert any("per-payment cap" in line for line in result.findings)
        finally:
            provider.close()
    finally:
        bounded.stop()


def test_preflight_refuses_a_supplier_the_accounting_system_blocked(tmp_path, chain):
    """The policy holds it, and the preflight refuses it independently of that."""
    settings, store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        fixture = store.get_supplier_fixture("SUP-ACME-001")
        store.seed_fixture(
            "supplier",
            "SUP-ACME-001",
            {**fixture, "payment_blocked": True, "blocked_reason": "the supplier is on hold"},
        )
        result = preflight(workflow, settings, invoice_id, rpc=RpcClient(chain.url))
        assert result.ok is False
        assert any("accounting system blocks this supplier" in line for line in result.findings)
    finally:
        provider.close()


def test_preflight_refuses_an_unlinked_invoice(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        invoice = store.get_invoice(invoice_id)
        unlinked = replace(invoice, id="unlinked-invoice", invoice_number="UNLINKED-1", purchase_invoice_id=None)
        store.create_invoice(unlinked, "unlinked-key")
        result = preflight(workflow, settings, "unlinked-invoice", rpc=RpcClient(chain.url))
        assert result.ok is False
        assert any("not linked to an accounting payable" in line for line in result.findings)
    finally:
        provider.close()


def test_preflight_refuses_a_provider_that_is_not_live(tmp_path, chain):
    settings, _store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        mock_settings = settings.model_copy(update={"payment_provider": "mock"})
        view = provider_view(mock_settings)
        assert view.live is False
        result = preflight(workflow, mock_settings, invoice_id, rpc=RpcClient(chain.url))
        assert result.ok is False
        assert any("no live payment would be sent" in line for line in result.findings)
    finally:
        provider.close()


def test_a_dry_run_reports_without_sending(tmp_path, chain):
    from arc_payables.live_run import run

    settings, store, provider, workflow, invoice_id = _workspace(tmp_path, chain)
    try:
        class Args:
            invoice = invoice_id
            confirm = False

        assert run(Args(), settings=settings) == 0
        # Nothing was signed: no payment exists for the invoice.
        assert store.get_payment(invoice_id) is None
    finally:
        provider.close()
