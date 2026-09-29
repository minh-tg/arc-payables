from __future__ import annotations

import httpx
import pytest

from tameion.domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC
from tameion.settings import Settings
from tameion.verify_arc import (
    DECIMALS_SELECTOR,
    EPOCH_CAP_SELECTOR,
    EPOCH_LENGTH_SELECTOR,
    PAYMENT_TOKEN_SELECTOR,
    PER_PAYMENT_CAP_SELECTOR,
    POLICY_SIGNER_SELECTOR,
    RECIPIENT_EPOCH_CAP_SELECTOR,
    RpcClient,
    provider_view,
    verify,
)

ZERO_PAD = "0" * 24
USDC_WORD = "0x" + ZERO_PAD + ARC_TESTNET_USDC[2:]
SIGNER = "0x" + "ab" * 20


def _string_result(text: str) -> str:
    offset = f"{32:064x}"
    length = f"{len(text):064x}"
    body = text.encode().hex().ljust(64, "0")
    return "0x" + offset + length + body


def _word(value: int) -> str:
    return f"{value:064x}"


def _rpc(
    chain_id: int = ARC_TESTNET_CHAIN_ID,
    decimals: int = 6,
    symbol: str = "USDC",
    guard_token: str = USDC_WORD,
    guard_signer: str = SIGNER,
    per_payment_cap: int = 1_000 * 10**6,
    epoch_cap: int = 10_000 * 10**6,
    recipient_cap: int = 5_000 * 10**6,
    epoch_length: int = 86_400,
):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode()
        if '"eth_chainId"' in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(chain_id)})
        if '"eth_getCode"' in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x6080"})
        if PER_PAYMENT_CAP_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(per_payment_cap)})
        if EPOCH_CAP_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(epoch_cap)})
        if RECIPIENT_EPOCH_CAP_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(recipient_cap)})
        if EPOCH_LENGTH_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(epoch_length)})
        if POLICY_SIGNER_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": guard_signer})
        if PAYMENT_TOKEN_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": guard_token})
        if DECIMALS_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": f"{decimals:064x}"})
        if '"eth_getBalance"' in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(10**18)})
        if "70a08231" in payload:  # balanceOf(address)
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _word(5_000 * 10**6)})
        if "95d89b41" in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": _string_result(symbol)})
        raise AssertionError(f"unexpected RPC request: {payload}")

    return RpcClient("https://rpc.invalid", httpx.Client(transport=httpx.MockTransport(handler)))


def _settings(**overrides) -> Settings:
    values = {"_env_file": None, "circle_rpc_url": "https://rpc.invalid"}
    values.update(overrides)
    return Settings(**values)


def test_verify_arc_accepts_the_documented_testnet_constants():
    ok, findings = verify(_rpc(), _settings())
    assert any("chain id is Arc Testnet" in line for line in findings)
    assert any("decimals() = 6" in line for line in findings)
    assert any("symbol() = USDC" in line for line in findings)
    # Live payment is still disabled with no Circle configuration, so overall not ready.
    assert ok is False
    assert any(line.startswith("TODO") for line in findings)


def test_verify_arc_rejects_a_non_testnet_chain():
    ok, findings = verify(_rpc(chain_id=1), _settings())
    assert ok is False
    assert any("refusing to use this endpoint" in line for line in findings)


def test_verify_arc_rejects_wrong_token_decimals_or_symbol():
    ok, findings = verify(_rpc(decimals=18), _settings())
    assert ok is False
    assert any("decimals() = 18" in line for line in findings)
    ok, findings = verify(_rpc(symbol="USDT"), _settings())
    assert ok is False
    assert any("expected 'USDC'" in line for line in findings)


def test_verify_arc_checks_guard_signer_and_token_against_configuration():
    settings = _settings(circle_guard_address="0x" + "22" * 20)
    ok, findings = verify(_rpc(), settings, signer_address=SIGNER)
    assert any("guard policySigner matches" in line for line in findings)
    assert any("guard paymentToken is the Arc Testnet USDC" in line for line in findings)
    assert any("guard budgets: per payment 1,000.00 USDC" in line for line in findings)

    _, mismatch = verify(_rpc(), settings, signer_address="0x" + "cd" * 20)
    assert any("policySigner does not match" in line for line in mismatch)

    wrong_token = "0x" + ZERO_PAD + "11" * 20
    _, token_findings = verify(_rpc(guard_token=wrong_token), settings, signer_address=SIGNER)
    assert any("guard paymentToken is 0x" in line for line in token_findings)


def test_a_guard_without_budget_caps_is_not_a_guarded_guard():
    settings = _settings(circle_guard_address="0x" + "22" * 20)
    ok, findings = verify(_rpc(per_payment_cap=0), settings, signer_address=SIGNER)
    assert ok is False
    assert any("no per-payment cap" in line for line in findings)

    ok, findings = verify(_rpc(epoch_cap=0), settings, signer_address=SIGNER)
    assert ok is False
    assert any("no per-epoch cap" in line for line in findings)


def test_an_impossible_budget_is_reported_as_a_failure():
    settings = _settings(circle_guard_address="0x" + "22" * 20)
    ok, findings = verify(_rpc(per_payment_cap=2_000 * 10**6, epoch_cap=1_000 * 10**6), settings, signer_address=SIGNER)
    assert ok is False
    assert any("smaller than the per-payment cap" in line for line in findings)


def test_a_configured_local_deployment_can_be_ready():
    """A local-key deployment with a guarded, budgeted contract and a funded payer is ready."""
    key = "0x" + "11" * 32
    from eth_account import Account

    wallet = Account.from_key(key).address
    settings = _settings(
        payment_provider="local",
        local_payment_private_key=key,
        local_payment_guard_address="0x" + "33" * 20,
        local_payment_rpc_url="https://rpc.invalid",
        permit_signing_private_key=key,
    )
    # The deployed guard must be the one this policy key signs for.
    guard_signer_word = "0x" + ZERO_PAD + wallet[2:]
    ok, findings = verify(
        _rpc(guard_signer=guard_signer_word), settings, signer_address=wallet, view=provider_view(settings)
    )
    assert any("local key (EOA)" in line for line in findings)
    assert any(wallet.lower() in line.lower() for line in findings)
    assert ok is True, findings


def test_the_local_provider_reports_what_is_missing():
    settings = _settings(payment_provider="local")
    view = provider_view(settings)
    assert view.name == "local" and view.configured is False
    ok, findings = verify(_rpc(), settings, view=view)
    assert ok is False
    assert any("LOCAL_PAYMENT_PRIVATE_KEY, LOCAL_PAYMENT_GUARD_ADDRESS" in line for line in findings)
