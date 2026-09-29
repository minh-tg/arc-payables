"""The local-key executor, against a real EVM running the real guard bytecode.

These exercise the same invariants as the Circle path, but with the payer key held locally:
exact payment, on-chain budget refusal, safe recovery after a crash, and a refusal to run
against anything that is not Arc Testnet.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from arc_payables.currency import USDCOnlyConverter
from arc_payables.domain import USDC_SCALE, WorkflowState
from arc_payables.fake_circle import DEFAULT_POLICY_KEY, AnvilChain
from arc_payables.local_payment import LocalKeyPaymentProvider, LocalPaymentError
from arc_payables.mock_adapters import MockAccountingConnector
from arc_payables.policy import DeterministicPolicy
from arc_payables.seed import APPROVED_WALLET, seed_demo
from arc_payables.security import EIP712PermitSigner
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

SUPPLIER = APPROVED_WALLET
INVOICE_USDC = 250


@pytest.fixture(scope="module")
def chain():
    with AnvilChain() as running:
        running.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        running.fund_native(running.wallet.address, 10**18)
        running.mint(running.wallet.address, 5_000 * USDC_SCALE)
        yield running


def _settings(chain, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "payment_provider": "local",
        "accounting_provider": "mock",
        "local_payment_private_key": chain.wallet.key.hex(),
        "local_payment_guard_address": chain.guard_address,
        "local_payment_rpc_url": chain.url,
        "local_payment_receipt_timeout_seconds": 30,
        "permit_signing_private_key": DEFAULT_POLICY_KEY,
    }
    values.update(overrides)
    return Settings(**values)


def _provider(chain, **overrides) -> LocalKeyPaymentProvider:
    settings = _settings(chain, **overrides)
    return LocalKeyPaymentProvider(settings, EIP712PermitSigner(DEFAULT_POLICY_KEY))


def _workflow(tmp: Path, chain, **overrides):
    settings = _settings(chain, **overrides)
    store = SQLiteEvidenceStore(tmp / "local.sqlite3")
    store.initialize()
    legitimate_id, _ = seed_demo(store)
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


def test_pays_the_supplier_exactly_and_reports_a_measured_fee(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        before = chain.erc20_balance(SUPPLIER)
        workflow.evaluate(invoice_id)
        result = workflow.submit_payment(invoice_id)
        assert result["state"] == WorkflowState.ERP_RECORDED.value
        payment = result["payment"]
        assert payment["confirmation_status"] == "CONFIRMED"

        # Exactly the authorized amount reached the supplier, and nothing more.
        assert chain.erc20_balance(SUPPLIER) - before == INVOICE_USDC * USDC_SCALE
        assert payment["transaction_hash"].startswith("0x")

        # The fee is measured from the receipt, in 6-decimal USDC units. The expected figure is
        # recomputed here from the chain's own receipt, so a wrong scale cannot pass unnoticed.
        stored = store.get_payment(invoice_id)
        receipt = provider._wait_receipt(stored["transaction_hash"])
        gas_wei = int(receipt["gasUsed"], 16) * int(receipt["effectiveGasPrice"], 16)
        expected_units = -(-gas_wei // 10**12)
        assert int(stored["fee_units"]) == expected_units
        assert expected_units > 1, "a gas cost this size must not collapse to a single micro-USDC"

        # The exact allowance the guard pulled is fully consumed, leaving nothing behind.
        assert provider._allowance() == 0
        assert provider._guard_used(stored["permit"]["payment_id"]) is True
    finally:
        provider.close()


def test_a_crash_after_broadcast_is_recovered_not_replayed(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        workflow.evaluate(invoice_id)
        workflow.submit_payment(invoice_id)
        stored = store.get_payment(invoice_id)
        settled = chain.erc20_balance(SUPPLIER)

        # Simulate losing everything except the authorization: no transaction id was recorded.
        recovery_input = {**stored, "provider_transaction_id": None, "provider_stage": None}
        restarted = _provider(chain)
        try:
            inspected = restarted.inspect_payment(recovery_input)
            assert inspected.status.value == "CONFIRMED"
            assert inspected.transaction_hash == stored["transaction_hash"]
            # And a fresh submission is refused rather than paying twice.
            retried = restarted.submit_authorized(recovery_input)
            assert retried.status.value in {"UNCERTAIN", "FAILED"}
            assert chain.erc20_balance(SUPPLIER) == settled
        finally:
            restarted.close()
    finally:
        provider.close()


def test_a_replayed_permit_is_refused_by_the_guard(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        workflow.evaluate(invoice_id)
        workflow.submit_payment(invoice_id)
        stored = store.get_payment(invoice_id)
        before = chain.erc20_balance(SUPPLIER)
        replay = provider.submit_authorized(stored)
        assert replay.status.value in {"UNCERTAIN", "FAILED"}
        assert replay.failure_code in {"ONCHAIN_PAYMENT_ALREADY_USED", "ONCHAIN_PAYMENT_USED_HASH_REQUIRES_RECONCILIATION"}
        assert chain.erc20_balance(SUPPLIER) == before
    finally:
        provider.close()


def test_an_expired_permit_is_refused_without_spending_gas(tmp_path, chain):
    """A permit past its expiry is refused from chain state, before any transaction is sent."""
    from arc_payables.domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC, PaymentPermit

    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        workflow.evaluate(invoice_id)
        before = chain.erc20_balance(SUPPLIER)
        signer = EIP712PermitSigner(DEFAULT_POLICY_KEY)
        permit = PaymentPermit(
            payer=chain.wallet.address,
            token=ARC_TESTNET_USDC,
            recipient=SUPPLIER,
            amount_units=INVOICE_USDC * USDC_SCALE,
            evidence_hash="0x" + "ab" * 32,
            payment_id="0x" + "ef" * 32,
            expiry=1_600_000_000,  # long past
            chain_id=ARC_TESTNET_CHAIN_ID,
            guard_address=chain.guard_address,
        )
        payment = {
            "payment_id": permit.payment_id,
            "permit": permit.to_dict(),
            "signature": signer.sign(permit),
        }
        result = provider.submit_authorized(payment)
        assert result.status.value == "FAILED"
        assert result.failure_code == "PERMIT_EXPIRED"
        assert chain.erc20_balance(SUPPLIER) == before
    finally:
        provider.close()


def test_a_paused_guard_is_refused_before_anything_is_sent(tmp_path, chain):
    """The pause control exists so an incident can be stopped without redeploying the guard."""
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        workflow.evaluate(invoice_id)
        before = chain.erc20_balance(SUPPLIER)
        chain.set_guard_paused(True)
        try:
            assert provider._guard_paused() is True
            permit = _fresh_permit(chain)
            result = provider.submit_authorized(permit)
            assert result.status.value == "FAILED"
            assert result.failure_code == "GUARD_PAUSED"
            # Nothing moved and no budget was consumed, which is the point of refusing early.
            assert chain.erc20_balance(SUPPLIER) == before
            assert chain.guard_used(permit["payment_id"]) is False
            assert chain.epoch_spent(0) == 0
        finally:
            chain.set_guard_paused(False)
        assert provider._guard_paused() is False
    finally:
        provider.close()


def _fresh_permit(chain) -> dict:
    """A signed, authorized payment that has not been submitted."""
    from arc_payables.domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC, PaymentPermit

    signer = EIP712PermitSigner(DEFAULT_POLICY_KEY)
    permit = PaymentPermit(
        payer=chain.wallet.address,
        token=ARC_TESTNET_USDC,
        recipient=SUPPLIER,
        amount_units=INVOICE_USDC * USDC_SCALE,
        evidence_hash="0x" + "cc" * 32,
        payment_id="0x" + "dd" * 32,
        expiry=2_000_000_000,
        chain_id=ARC_TESTNET_CHAIN_ID,
        guard_address=chain.guard_address,
    )
    return {
        "payment_id": permit.payment_id,
        "permit": permit.to_dict(),
        "signature": signer.sign(permit),
    }


def test_on_chain_budget_refuses_a_payment_the_policy_approves(tmp_path):
    """The policy allows 250 USDC; a 100 USDC contract cap refuses it, naming the cap."""
    with AnvilChain() as bounded:
        bounded.deploy_suite(
            EIP712PermitSigner(DEFAULT_POLICY_KEY).address,
            per_payment_cap=100 * USDC_SCALE,
            epoch_cap=500 * USDC_SCALE,
            recipient_epoch_cap=500 * USDC_SCALE,
        )
        bounded.fund_native(bounded.wallet.address, 10**18)
        bounded.mint(bounded.wallet.address, 5_000 * USDC_SCALE)
        settings, store, provider, workflow, invoice_id = _workflow(tmp_path, bounded)
        try:
            evaluated = workflow.evaluate(invoice_id)
            assert evaluated["state"] == WorkflowState.ELIGIBLE.value
            assert settings.max_invoice_usdc == Decimal("1000")
            before = bounded.erc20_balance(SUPPLIER)

            result = workflow.submit_payment(invoice_id)

            assert result["state"] == WorkflowState.FAILED.value
            assert result["payment"]["failure_code"] == "GUARD_PER_PAYMENT_CAP_EXCEEDED"
            assert bounded.erc20_balance(SUPPLIER) == before
            assert bounded.epoch_spent(0) == 0
            assert store.get_payment(invoice_id)["erp_status"] != "RECORDED"
        finally:
            provider.close()


def test_insufficient_balance_fails_before_broadcasting(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        workflow.evaluate(invoice_id)
        before = chain.erc20_balance(SUPPLIER)
        drained = chain.erc20_balance(chain.wallet.address)
        chain.transfer("0x00000000000000000000000000000000000000ff", drained)
        try:
            from arc_payables.domain import PaymentPermit
            from arc_payables.domain import ARC_TESTNET_USDC, ARC_TESTNET_CHAIN_ID

            permit = PaymentPermit(
                payer=chain.wallet.address,
                token=ARC_TESTNET_USDC,
                recipient=SUPPLIER,
                amount_units=INVOICE_USDC * USDC_SCALE,
                evidence_hash="0x" + "ab" * 32,
                payment_id="0x" + "cd" * 32,
                expiry=2_000_000_000,
                chain_id=ARC_TESTNET_CHAIN_ID,
                guard_address=chain.guard_address,
            )
            payment = {
                "payment_id": permit.payment_id,
                "permit": permit.to_dict(),
                "signature": EIP712PermitSigner(DEFAULT_POLICY_KEY).sign(permit),
            }
            result = provider.submit_authorized(payment)
            assert result.status.value == "FAILED"
            assert result.failure_code == "INSUFFICIENT_USDC_BALANCE"
            # Nothing was broadcast, so the supplier balance is untouched.
            assert chain.erc20_balance(SUPPLIER) == before
        finally:
            chain.mint(chain.wallet.address, 5_000 * USDC_SCALE)
    finally:
        provider.close()


def test_refuses_a_node_that_is_not_arc_testnet(tmp_path):
    """A mistyped RPC URL must not silently send a payment to another chain."""
    with AnvilChain(chain_id=31_337) as other:
        other.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        other.fund_native(other.wallet.address, 10**18)
        provider = _provider(other)
        try:
            with pytest.raises(LocalPaymentError, match="not Arc Testnet"):
                provider.get_balance()
        finally:
            provider.close()


def test_reported_budget_shrinks_after_a_payment(tmp_path, chain):
    settings, store, provider, workflow, invoice_id = _workflow(tmp_path, chain)
    try:
        before = provider.remaining_budget(SUPPLIER)
        assert before is not None and before > 0
        workflow.evaluate(invoice_id)
        workflow.submit_payment(invoice_id)
        after = provider.remaining_budget(SUPPLIER)
        assert after == before - INVOICE_USDC * USDC_SCALE
    finally:
        provider.close()


def test_incomplete_local_configuration_fails_closed(tmp_path):
    from fastapi.testclient import TestClient

    from arc_payables.api import DisabledPaymentProvider, create_app

    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "unconfigured.sqlite3",
        payment_provider="local",
        api_key="test-key",
    )
    app = create_app(settings=settings)
    assert isinstance(app.state.workflow.payment_provider, DisabledPaymentProvider)
    response = TestClient(app).get("/ready")
    assert response.status_code == 503
    assert response.json()["detail"]["payment_provider_configured"] is False


def test_native_gas_is_converted_to_six_decimal_units():
    """Arc reports gas in 18-decimal USDC while the token has 6 decimals.

    Getting this wrong by a factor of 10**6 turns a real fee into "1", which is exactly what the
    first live Arc Testnet payment reported. The numbers below are a real receipt's.
    """
    convert = LocalKeyPaymentProvider._fee_units
    # A real Arc Testnet receipt: 166,096 gas at 21 gwei.
    assert convert(3_488_016_000_000_000) == 3489   # 0.003488016 USDC, rounded up
    assert convert(10**12) == 1                     # the smallest representable unit
    assert convert(10**12 - 1) == 1                 # rounded up, never down to zero
    assert convert(1) == 1
    assert convert(0) == 0
