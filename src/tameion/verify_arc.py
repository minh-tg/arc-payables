"""Credential-free verification of the hardcoded Arc Testnet assumptions.

This runs read-only JSON-RPC calls only. It never signs, never submits a transaction,
and never needs a Circle API key or a funded wallet, so it is safe to run against the
public Arc Testnet endpoint at any time.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx

from .domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC
from .evm import decode_uint256, encode_balance_of, selector
from .settings import Settings, get_settings

DECIMALS_SELECTOR = "0x" + selector("decimals()").hex()
SYMBOL_SELECTOR = "0x" + selector("symbol()").hex()
POLICY_SIGNER_SELECTOR = "0x" + selector("policySigner()").hex()
PAYMENT_TOKEN_SELECTOR = "0x" + selector("paymentToken()").hex()


class RpcClient:
    def __init__(self, url: str, client: httpx.Client | None = None):
        self.url = url
        self.client = client or httpx.Client(timeout=20.0)

    def call(self, method: str, params: list) -> Any:
        response = self.client.post(self.url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise RuntimeError(f"RPC {method} returned an error: {payload['error'].get('message', 'unknown')}")
        return payload["result"]


def _address_from_word(data: str) -> str:
    return "0x" + data[-40:]


def verify(rpc: RpcClient, settings: Settings, *, signer_address: str | None = None) -> tuple[bool, list[str]]:
    findings: list[str] = []
    ok = True

    chain_id = int(rpc.call("eth_chainId", []), 16)
    if chain_id == ARC_TESTNET_CHAIN_ID:
        findings.append(f"OK    chain id is Arc Testnet ({chain_id})")
    else:
        ok = False
        findings.append(f"FAIL  chain id is {chain_id}, expected {ARC_TESTNET_CHAIN_ID}; refusing to use this endpoint")

    code = rpc.call("eth_getCode", [ARC_TESTNET_USDC, "latest"])
    if isinstance(code, str) and code not in {"0x", "0x0"}:
        findings.append(f"OK    USDC contract {ARC_TESTNET_USDC} has bytecode")
    else:
        ok = False
        findings.append(f"FAIL  no contract bytecode at {ARC_TESTNET_USDC}")

    decimals = decode_uint256(rpc.call("eth_call", [{"to": ARC_TESTNET_USDC, "data": DECIMALS_SELECTOR}, "latest"]))
    if decimals == 6:
        findings.append("OK    USDC ERC-20 decimals() = 6")
    else:
        ok = False
        findings.append(f"FAIL  USDC decimals() = {decimals}, expected 6")

    symbol = _decode_string(rpc.call("eth_call", [{"to": ARC_TESTNET_USDC, "data": SYMBOL_SELECTOR}, "latest"]))
    if symbol == "USDC":
        findings.append("OK    USDC ERC-20 symbol() = USDC")
    else:
        ok = False
        findings.append(f"FAIL  USDC symbol() = {symbol!r}, expected 'USDC'")

    if settings.circle_guard_address:
        signer_word = rpc.call("eth_call", [{"to": settings.circle_guard_address, "data": POLICY_SIGNER_SELECTOR}, "latest"])
        token_word = rpc.call("eth_call", [{"to": settings.circle_guard_address, "data": PAYMENT_TOKEN_SELECTOR}, "latest"])
        onchain_signer = _address_from_word(signer_word)
        onchain_token = _address_from_word(token_word)
        if signer_address and onchain_signer.lower() != signer_address.lower():
            ok = False
            findings.append("FAIL  guard policySigner does not match the configured permit signing key")
        else:
            findings.append("OK    guard policySigner matches configuration")
        if onchain_token.lower() != ARC_TESTNET_USDC.lower():
            ok = False
            findings.append(f"FAIL  guard paymentToken is {onchain_token}, expected {ARC_TESTNET_USDC}")
        else:
            findings.append("OK    guard paymentToken is the Arc Testnet USDC address")
    else:
        findings.append("TODO  CIRCLE_GUARD_ADDRESS is not configured; the guard is not deployed or not recorded")

    if settings.circle_wallet_address:
        native = int(rpc.call("eth_getBalance", [settings.circle_wallet_address, "latest"]), 16)
        findings.append(f"INFO  configured Circle wallet native balance is {native} wei (Arc native USDC pays gas at 18 decimals)")
        erc20 = decode_uint256(
            rpc.call("eth_call", [{"to": ARC_TESTNET_USDC, "data": encode_balance_of(settings.circle_wallet_address)}, "latest"])
        )
        findings.append(f"INFO  configured Circle wallet ERC-20 USDC balance is {erc20} units (6 decimals)")
    else:
        findings.append("TODO  CIRCLE_WALLET_ADDRESS is not configured; no wallet balance was checked")

    if not settings.circle_ready:
        ok = False
        findings.append("TODO  live payment is disabled until every CIRCLE_*/PERMIT_SIGNING_PRIVATE_KEY setting is present")
    return ok, findings


def _decode_string(data: str) -> str:
    raw = bytes.fromhex(data.removeprefix("0x"))
    if len(raw) < 64:
        return ""
    # ABI-encoded string: offset word, length word, then UTF-8 bytes.
    offset = int.from_bytes(raw[:32], "big")
    length = int.from_bytes(raw[offset : offset + 32], "big")
    return raw[offset + 32 : offset + 32 + length].decode("utf-8", errors="replace")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify Arc Testnet configuration (read-only, no credentials)")
    parser.add_argument("--rpc-url", default=None, help="Override CIRCLE_RPC_URL for this check")
    args = parser.parse_args()
    settings = get_settings()
    rpc = RpcClient(args.rpc_url or settings.circle_rpc_url)
    signer_address = None
    if settings.permit_signing_private_key:
        from .security import EIP712PermitSigner

        signer_address = EIP712PermitSigner(settings.permit_signing_private_key).address
    try:
        ok, findings = verify(rpc, settings, signer_address=signer_address)
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"FAIL  could not complete Arc verification: {type(exc).__name__}: {exc}")
        raise SystemExit(2) from exc
    print(f"Arc Testnet verification ({settings.circle_rpc_url})")
    for line in findings:
        print(f"  {line}")
    print("Result: " + ("all checks passed" if ok else "not ready for a live payment"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
