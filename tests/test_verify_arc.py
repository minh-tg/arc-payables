from __future__ import annotations

import httpx
import pytest

from tameion.domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC
from tameion.settings import Settings
from tameion.verify_arc import DECIMALS_SELECTOR, PAYMENT_TOKEN_SELECTOR, POLICY_SIGNER_SELECTOR, RpcClient, verify

ZERO_PAD = "0" * 24
USDC_WORD = "0x" + ZERO_PAD + ARC_TESTNET_USDC[2:]
SIGNER = "0x" + "ab" * 20


def _string_result(text: str) -> str:
    offset = f"{32:064x}"
    length = f"{len(text):064x}"
    body = text.encode().hex().ljust(64, "0")
    return "0x" + offset + length + body


def _rpc(chain_id: int = ARC_TESTNET_CHAIN_ID, decimals: int = 6, symbol: str = "USDC", guard_token: str = USDC_WORD, guard_signer: str = SIGNER):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode()
        if '"eth_chainId"' in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(chain_id)})
        if '"eth_getCode"' in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x6080"})
        if POLICY_SIGNER_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": guard_signer})
        if PAYMENT_TOKEN_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": guard_token})
        if DECIMALS_SELECTOR in payload:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": f"{decimals:064x}"})
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

    _, mismatch = verify(_rpc(), settings, signer_address="0x" + "cd" * 20)
    assert any("policySigner does not match" in line for line in mismatch)

    wrong_token = "0x" + ZERO_PAD + "11" * 20
    _, token_findings = verify(_rpc(guard_token=wrong_token), settings, signer_address=SIGNER)
    assert any("guard paymentToken is 0x" in line for line in token_findings)
