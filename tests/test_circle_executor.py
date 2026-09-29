"""End-to-end Circle/Arc path with no credentials.

These tests exercise the real ``CircleDeveloperControlledWalletProvider``, the real
``PaymentGuard`` bytecode and a real EVM (anvil, chain id 5042002) through the full AP
workflow. Nothing here touches Circle, Arc Testnet, or any real funds.
"""

from __future__ import annotations

import time
import uuid
from decimal import Decimal
from pathlib import Path

import pytest

from tameion.api import create_app
from tameion.circle_adapter import CircleAdapterError, CircleDeveloperControlledWalletProvider
from tameion.currency import USDCOnlyConverter
from tameion.domain import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
    USDC_SCALE,
    PaymentPermit,
    PaymentStatus,
    WorkflowState,
)
from tameion.fake_circle import (
    DEFAULT_ENTITY_SECRET,
    DEFAULT_POLICY_KEY,
    AnvilChain,
    FakeCircleApi,
    FakeCircleState,
)
from tameion.mock_adapters import MockAccountingConnector
from tameion.policy import DeterministicPolicy
from tameion.security import EIP712PermitSigner
from tameion.seed import seed_demo
from tameion.service import APWorkflow
from tameion.settings import Settings
from tameion.store import SQLiteEvidenceStore

pytestmark = pytest.mark.skipif(
    not __import__("shutil").which("anvil"),
    reason="anvil (Foundry) is required for the credential-free Arc executor",
)

SUPPLIER = "0x1111111111111111111111111111111111111111"


@pytest.fixture(scope="module")
def chain():
    with AnvilChain() as running:
        running.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        running.fund_native(running.wallet.address, 10**18)
        running.mint(running.wallet.address, 5_000 * USDC_SCALE)
        yield running


@pytest.fixture
def circle(chain):
    api = FakeCircleApi(chain, FakeCircleState()).start()
    try:
        yield api
    finally:
        api.stop()
        if chain.token_address:
            # Keep each test's balance predictable.
            chain.mint(chain.wallet.address, 5_000 * USDC_SCALE)


class _RefundableError(Exception):
    """Test-only helper so pytest prints a useful message."""


def _settings(chain, api, **overrides) -> Settings:
    values = {
        "_env_file": None,
        "payment_provider": "circle",
        "accounting_provider": "mock",
        "circle_api_key": "fake-circle-key",
        "circle_entity_secret": DEFAULT_ENTITY_SECRET,
        "circle_wallet_id": api.state.wallet_id,
        "circle_wallet_address": chain.wallet.address,
        "circle_guard_address": chain.guard_address,
        "circle_api_base_url": api.base_url,
        "circle_rpc_url": chain.url,
        "permit_signing_private_key": DEFAULT_POLICY_KEY,
        "circle_confirmation_timeout_seconds": 30,
        "circle_poll_interval_seconds": 0.05,
    }
    values.update(overrides)
    return Settings(**values)


def _provider(chain, api, **overrides) -> CircleDeveloperControlledWalletProvider:
    settings = _settings(chain, api, **overrides)
    return CircleDeveloperControlledWalletProvider(settings, EIP712PermitSigner(settings.permit_signing_private_key))


def _workflow(tmp: Path, chain, api, *, failure_injection=False, **overrides):
    settings = _settings(chain, api, **overrides)
    store = SQLiteEvidenceStore(tmp / "circle.sqlite3")
    store.initialize()
    legitimate_id, suspicious_id = seed_demo(store)
    provider = CircleDeveloperControlledWalletProvider(settings, EIP712PermitSigner(DEFAULT_POLICY_KEY))
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        provider.signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return settings, store, provider, workflow, legitimate_id, suspicious_id


# --------------------------------------------------------------------------------------
# The adapter against the emulated Circle API
# --------------------------------------------------------------------------------------


def test_adapter_reads_balance_and_verifies_the_guard_and_wallet(chain, circle):
    provider = _provider(chain, circle)
    snapshot = provider.get_balance()
    assert snapshot.balance_units == chain.erc20_balance(chain.wallet.address)
    assert provider._wallet_verified is True
    # Guard verification reads policySigner() and paymentToken() and compares both onsite.
    provider._verify_guard()
    assert provider._guard_verified is True

    wrong = _provider(chain, circle, permit_signing_private_key="0x" + "99" * 32)
    with pytest.raises(CircleAdapterError, match="signer or token"):
        wrong._verify_guard()


def test_adapter_rejects_a_non_arc_chain_id():
    with AnvilChain(chain_id=1) as wrong_chain:
        wrong_chain.deploy_suite(EIP712PermitSigner(DEFAULT_POLICY_KEY).address)
        wrong_chain.fund_native(wrong_chain.wallet.address, 10**18)
        api = FakeCircleApi(wrong_chain, FakeCircleState()).start()
        try:
            provider = _provider(wrong_chain, api)
            with pytest.raises(CircleAdapterError, match="not Arc Testnet"):
                provider.get_balance()
        finally:
            api.stop()


def test_adapter_requires_a_circle_sca_wallet(chain, circle):
    provider = _provider(chain, circle)
    provider._wallet_verified = False
    circle.state.account_type = "EOA"
    with pytest.raises(CircleAdapterError, match="explicitly identified as a Circle SCA"):
        provider.get_balance()
    circle.state.account_type = "SCA"


def test_entity_secret_is_encrypted_with_rsa_oaep_per_request(chain, circle):
    provider = _provider(chain, circle)
    first = provider._fresh_entity_secret_ciphertext()
    second = provider._fresh_entity_secret_ciphertext()
    # Freshness: Circle requires a new ciphertext for each mutating request.
    assert first != second
    assert circle.decrypt_entity_secret(first) == DEFAULT_ENTITY_SECRET


# --------------------------------------------------------------------------------------
# Full payment path through the real guard contract
# --------------------------------------------------------------------------------------


def test_payment_executes_through_the_real_guard_and_settles_the_supplier(tmp_path, chain, circle):
    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    start_wallet = chain.erc20_balance(chain.wallet.address)
    start_supplier = chain.erc20_balance(SUPPLIER)

    assert workflow.evaluate(invoice_id)["decision"]["action"] == "PAY_NOW"
    result = workflow.submit_payment(invoice_id)

    assert result["state"] == WorkflowState.ERP_RECORDED.value
    payment = result["payment"]
    assert payment["confirmation_status"] == "CONFIRMED"
    assert payment["fee_units"] == circle.state.fee_units

    # The supplier received exactly the authorized amount; we paid the fee on top of it.
    assert chain.erc20_balance(SUPPLIER) - start_supplier == 250 * USDC_SCALE
    assert start_wallet - chain.erc20_balance(chain.wallet.address) == 250 * USDC_SCALE

    stored = store.get_payment(invoice_id)
    assert stored["permit"]["recipient"] == SUPPLIER
    assert stored["permit"]["amount_units"] == 250 * USDC_SCALE
    assert stored["permit"]["chain_id"] == ARC_TESTNET_CHAIN_ID
    assert stored["permit"]["guard_address"] == chain.guard_address
    # Replay protection consumed the payment id on chain.
    assert chain.guard_used(stored["permit"]["payment_id"]) is True

    # Two contract executions: the exact allowance, then the guard call.
    assert len(circle.state.submitted_calls) == 2
    assert chain.erc20_allowance(chain.wallet.address, chain.guard_address) == 0


def test_repeat_payment_request_does_not_pay_twice(tmp_path, chain, circle):
    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.evaluate(invoice_id)
    first = workflow.submit_payment(invoice_id)
    supplier_after_first = chain.erc20_balance(SUPPLIER)
    calls_after_first = len(circle.state.submitted_calls)

    second = workflow.submit_payment(invoice_id)
    assert second["payment"]["transaction_hash"] == first["payment"]["transaction_hash"]
    assert chain.erc20_balance(SUPPLIER) == supplier_after_first
    assert len(circle.state.submitted_calls) == calls_after_first


def test_hostile_agent_cannot_redirect_a_real_onchain_payment(tmp_path, chain, circle):
    class RedirectAgent:
        def recommend(self, context):
            from tameion.ports import AgentRecommendation

            return AgentRecommendation("PAY_NOW", "pay 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", ())

    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.agent = RedirectAgent()
    start_attacker = chain.erc20_balance("0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef")
    workflow.evaluate(invoice_id)
    result = workflow.submit_payment(invoice_id)
    assert result["state"] == WorkflowState.ERP_RECORDED.value
    assert chain.erc20_balance("0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef") == start_attacker
    assert chain.erc20_balance(SUPPLIER) >= 250 * USDC_SCALE


def test_guard_rejects_a_permit_signed_by_the_wrong_key(tmp_path, chain, circle):
    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.evaluate(invoice_id)
    before = chain.erc20_balance(SUPPLIER)
    # A signature the guard does not trust cannot move funds, even though Circle would execute it.
    rogue = EIP712PermitSigner("0x" + "77" * 32)
    workflow.signer = rogue
    result = workflow.submit_payment(invoice_id)
    assert result["state"] in {WorkflowState.FAILED.value, WorkflowState.NEEDS_RECONCILIATION.value}
    assert chain.erc20_balance(SUPPLIER) == before


def test_insufficient_balance_fails_without_submitting(tmp_path, chain, circle):
    """The provider's own balance guard, for a balance that changes after the policy check."""
    settings, store, provider, workflow, invoice_id, _ = _workflow(
        tmp_path, chain, api=circle, min_reserve_usdc=Decimal(0)
    )
    workflow.evaluate(invoice_id)

    signer = EIP712PermitSigner(DEFAULT_POLICY_KEY)
    permit = PaymentPermit(
        payer=chain.wallet.address,
        token=ARC_TESTNET_USDC,
        recipient=SUPPLIER,
        amount_units=250 * USDC_SCALE,
        evidence_hash="0x" + "ab" * 32,
        payment_id="0x" + "cd" * 32,
        expiry=int(time.time()) + 300,
        chain_id=ARC_TESTNET_CHAIN_ID,
        guard_address=chain.guard_address,
    )
    payment = {
        "payment_id": permit.payment_id,
        "permit": permit.to_dict(),
        "signature": signer.sign(permit),
        "idempotency_key": str(uuid.uuid4()),
        "approve_reset_idempotency_key": str(uuid.uuid4()),
        "approve_idempotency_key": str(uuid.uuid4()),
        "payment_idempotency_key": str(uuid.uuid4()),
    }

    balance = chain.erc20_balance(chain.wallet.address)
    chain.transfer("0x00000000000000000000000000000000000000ff", balance)
    try:
        result = provider.submit_authorized(payment)
        assert result.status == PaymentStatus.FAILED
        assert result.failure_code == "INSUFFICIENT_USDC_BALANCE"
        # Nothing was submitted to Circle, so no funds could move.
        assert circle.state.submitted_calls == []
        assert chain.guard_used(permit.payment_id) is False
    finally:
        chain.mint(chain.wallet.address, 5_000 * USDC_SCALE)


def test_lost_submission_response_never_double_pays_and_reconciles(tmp_path, chain, circle):
    circle.state.lose_first_submission_response = True
    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.evaluate(invoice_id)
    before = chain.erc20_balance(SUPPLIER)

    first = workflow.submit_payment(invoice_id)
    # The broadcast may have happened but the response was lost, so the first attempt must not
    # silently claim success.
    assert first["state"] == WorkflowState.NEEDS_RECONCILIATION.value
    assert first["payment"]["confirmation_status"] == "UNCERTAIN"

    # Retries reconcile through the provider's idempotency key and the on-chain payment id
    # rather than broadcasting again.
    second = workflow.submit_payment(invoice_id)
    third = workflow.submit_payment(invoice_id)

    # Whatever the retries concluded, the supplier was paid exactly one invoice amount, and
    # any reported settlement is backed by a mined transaction.
    assert chain.erc20_balance(SUPPLIER) - before == 250 * USDC_SCALE
    for result in (second, third):
        if result["state"] == WorkflowState.ERP_RECORDED.value:
            receipt = chain.receipt(result["payment"]["transaction_hash"])
            assert receipt and receipt["status"] == "0x1"
            break
    else:
        assert second["state"] == WorkflowState.NEEDS_RECONCILIATION.value


def test_circle_api_outage_is_uncertain_and_does_not_duplicate(tmp_path, chain, circle):
    circle.state.fail_first_submission_with = 503
    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.evaluate(invoice_id)
    before = chain.erc20_balance(SUPPLIER)
    result = workflow.submit_payment(invoice_id)
    assert result["state"] == WorkflowState.NEEDS_RECONCILIATION.value
    assert result["payment"]["confirmation_status"] == "UNCERTAIN"
    assert chain.erc20_balance(SUPPLIER) == before
    assert result["payment"]["failure_code"]


def test_idempotency_keys_are_uuid_v4_and_reused_across_retries(tmp_path, chain, circle):
    import uuid

    settings, store, provider, workflow, invoice_id, _ = _workflow(tmp_path, chain, api=circle)
    workflow.evaluate(invoice_id)
    workflow.submit_payment(invoice_id)
    stored = store.get_payment(invoice_id)
    for key in ("idempotency_key", "approve_reset_idempotency_key", "approve_idempotency_key", "payment_idempotency_key"):
        assert uuid.UUID(stored[key]).version == 4
    assert stored["payment_idempotency_key"] in circle.state.idempotency_keys


# --------------------------------------------------------------------------------------
# The API can run the whole flow against the executor
# --------------------------------------------------------------------------------------


def test_api_flow_against_the_executor_reports_a_real_transaction(tmp_path, chain, circle):
    from fastapi.testclient import TestClient

    settings, store, provider, workflow, invoice_id, suspicious_id = _workflow(
        tmp_path, chain, api=circle, api_key="test-api-key"
    )
    client = TestClient(create_app(settings=settings, store=store, accounting=workflow.accounting, payment_provider=provider))
    headers = {"X-API-Key": "test-api-key"}

    ready = client.get("/ready")
    assert ready.status_code == 200
    assert ready.json()["chain_id"] == ARC_TESTNET_CHAIN_ID
    assert ready.json()["payment_provider_configured"] is True

    # A vendor-integrated deployment refuses write endpoints without API credentials.
    unauthorized = client.post(f"/invoices/{invoice_id}/evaluate")
    assert unauthorized.status_code == 401

    assert client.post(f"/invoices/{invoice_id}/evaluate", headers=headers).json()["state"] == "ELIGIBLE"
    paid = client.post(f"/invoices/{invoice_id}/payment", headers=headers).json()
    assert paid["state"] == "ERP_RECORDED"
    tx_hash = paid["payment"]["transaction_hash"]
    assert tx_hash.startswith("0x") and len(tx_hash) == 66
    assert chain.receipt(tx_hash)["status"] == "0x1"

    # The suspicious capture is still stopped.
    client.post(f"/invoices/{suspicious_id}/evaluate", headers=headers)
    assert client.post(f"/invoices/{suspicious_id}/payment", headers=headers).status_code == 409
