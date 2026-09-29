from __future__ import annotations

import base64

import httpx
import pytest
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA

from arc_payables.circle_adapter import CircleAdapterError, CircleDeveloperControlledWalletProvider
from arc_payables.crypto_utils import encrypt_circle_entity_secret
from arc_payables.security import EIP712PermitSigner
from arc_payables.settings import Settings


def _provider(rpc_handler=None, api_handler=None):
    settings = Settings(
        _env_file=None,
        payment_provider="circle",
        circle_api_key="test-api-key",
        circle_entity_secret="11" * 32,
        circle_wallet_id="test-wallet-id",
        circle_wallet_address="0x1111111111111111111111111111111111111111",
        circle_guard_address="0x2222222222222222222222222222222222222222",
        permit_signing_private_key="0x" + "01".zfill(64),
    )
    rpc_client = httpx.Client(transport=httpx.MockTransport(rpc_handler or (lambda request: httpx.Response(200, json={"result": "0x4d12b2"}))))
    api_client = httpx.Client(transport=httpx.MockTransport(api_handler or (lambda request: httpx.Response(200, json={"data": {}}))))
    signer = EIP712PermitSigner(settings.permit_signing_private_key)
    return CircleDeveloperControlledWalletProvider(settings, signer, client=api_client, rpc_client=rpc_client)


def test_entity_secret_encryption_uses_rsa_oaep_sha256():
    rsa_key = RSA.generate(2048)
    secret = bytes.fromhex("ab" * 32)
    ciphertext = encrypt_circle_entity_secret(secret.hex(), rsa_key.publickey().export_key().decode())
    encrypted_bytes = base64.b64decode(ciphertext)
    cipher = PKCS1_OAEP.new(rsa_key, hashAlgo=SHA256)
    assert cipher.decrypt(encrypted_bytes) == secret


def test_circle_refuses_any_chain_other_than_arc_testnet():
    def wrong_chain(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/"
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x1"})

    provider = _provider(rpc_handler=wrong_chain)
    with pytest.raises(CircleAdapterError, match="not Arc Testnet"):
        provider.get_balance()


def test_circle_status_requires_complete_and_never_guesses_fee_denomination():
    responses = [
        httpx.Response(200, json={"data": {"transaction": {"state": "CONFIRMED", "txHash": "0xabc", "networkFee": "0.01"}}}),
        httpx.Response(200, json={"data": {"transaction": {"state": "COMPLETE", "txHash": "0xdef", "networkFee": "0.01"}}}),
        httpx.Response(200, json={"data": {"transaction": {"state": "COMPLETE", "txHash": "0x123", "networkFeeUsdc": {"currency": "USDC", "decimals": 6, "amount": "0.01"}}}}),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    provider = _provider(api_handler=handler)
    pending = provider._circle_status("tx-1")
    assert pending.status.value == "PENDING"
    assert pending.fee_units is None
    complete_ambiguous = provider._circle_status("tx-2")
    assert complete_ambiguous.status.value == "CONFIRMED"
    assert complete_ambiguous.fee_units is None
    complete_explicit = provider._circle_status("tx-3")
    assert complete_explicit.fee_units == 10_000


def test_circle_post_5xx_is_uncertain_but_read_failure_is_not_a_submission():
    def unavailable(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "unavailable"})

    provider = _provider(api_handler=unavailable)
    with pytest.raises(CircleAdapterError) as post_error:
        provider._api_request("POST", "/developer/transactions/contractExecution", json_body={})
    assert post_error.value.uncertain
    with pytest.raises(CircleAdapterError) as get_error:
        provider._api_request("GET", "/wallets/test-wallet-id")
    assert not get_error.value.uncertain
