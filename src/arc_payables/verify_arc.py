"""Credential-free verification of the hardcoded Arc Testnet assumptions.

This runs read-only JSON-RPC calls only. It never signs, never submits a transaction, and
never needs a Circle API key or a funded wallet, so it is safe to run against the public Arc
Testnet endpoint at any time.

It answers one question: *is this deployment actually ready to send a real payment, and if
not, what exactly is missing?* That includes the deployed guard's budget caps, because a
guard with no cap configured is not a guarded guard.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx
from eth_account import Account
from eth_utils import keccak

from .domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC, USDC_SCALE
from .evm import decode_bool, decode_uint256, encode_balance_of, selector
from .settings import Settings, get_settings

DECIMALS_SELECTOR = "0x" + selector("decimals()").hex()
SYMBOL_SELECTOR = "0x" + selector("symbol()").hex()
POLICY_SIGNER_SELECTOR = "0x" + selector("policySigner()").hex()
PAYMENT_TOKEN_SELECTOR = "0x" + selector("paymentToken()").hex()
PER_PAYMENT_CAP_SELECTOR = "0x" + selector("perPaymentCap()").hex()
EPOCH_CAP_SELECTOR = "0x" + selector("epochCap()").hex()
RECIPIENT_EPOCH_CAP_SELECTOR = "0x" + selector("recipientEpochCap()").hex()
EPOCH_LENGTH_SELECTOR = "0x" + selector("epochLength()").hex()
PAUSED_SELECTOR = "0x" + selector("paused()").hex()

# Standard EntryPoint v0.7 UserOperationEvent. The paymaster is the third indexed argument
# (userOpHash, sender, paymaster, ...), so it travels as topics[3], the low 20 bytes of the word.
USER_OPERATION_EVENT_TOPIC = "0x" + keccak(
    text="UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)"
).hex()
ARC_TESTNET_ENTRYPOINT = "0x0000000071727de22e5e9d8baf0edac6f37da032"
ZERO_ADDRESS = "0x" + "00" * 20
# The documented Circle SCA settlement in docs/evidence/arc-testnet-allowance-history.json.
DEFAULT_PAYMASTER_TRANSACTION_HASH = "0x007678e3a08575390a39b8a77ac7598847db3ca7f0fb8a01eae39a1b61741db0"

LIVE_PROVIDERS = {"circle", "local"}


@dataclass(frozen=True)
class ProviderView:
    """Which deployment is being verified, and where its details live."""

    name: str
    rpc_url: str
    guard_address: str | None
    wallet_address: str | None
    configured: bool
    configuration_note: str

    @property
    def live(self) -> bool:
        return self.name in LIVE_PROVIDERS

    @property
    def label(self) -> str:
        return {"circle": "Circle Developer-Controlled Wallets", "local": "local key (EOA)", "mock": "mock"}.get(
            self.name, self.name
        )


def provider_view(settings: Settings) -> ProviderView:
    """Resolve the configured provider into the addresses worth verifying.

    The mock and circle providers both read the CIRCLE_* settings, which is what an
    unconfigured demo has; the local key provider reads its own.
    """
    if settings.payment_provider == "local":
        wallet = settings.local_payment_address
        if not wallet and settings.local_payment_private_key:
            try:
                wallet = Account.from_key(settings.local_payment_private_key).address
            except Exception:
                wallet = None
        missing = [
            name
            for name, value in (
                ("LOCAL_PAYMENT_PRIVATE_KEY", settings.local_payment_private_key),
                ("LOCAL_PAYMENT_GUARD_ADDRESS", settings.local_payment_guard_address),
            )
            if not value
        ]
        return ProviderView(
            "local",
            settings.local_payment_rpc_url,
            settings.local_payment_guard_address,
            wallet,
            settings.local_payment_ready,
            "missing " + ", ".join(missing) if missing else "configured",
        )
    view = ProviderView(
        "circle" if settings.payment_provider == "circle" else "mock",
        settings.circle_rpc_url,
        settings.circle_guard_address,
        settings.circle_wallet_address,
        settings.circle_ready if settings.payment_provider == "circle" else True,
        "configured" if settings.payment_provider == "circle" else "the mock provider sends nothing",
    )
    return view


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


def _usdc(units: int) -> str:
    return f"{units / USDC_SCALE:,.2f}"


def verify(
    rpc: RpcClient,
    settings: Settings,
    *,
    signer_address: str | None = None,
    view: ProviderView | None = None,
) -> tuple[bool, list[str]]:
    view = view or provider_view(settings)
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

    guard_ok = True
    if view.guard_address:
        signer_word = rpc.call("eth_call", [{"to": view.guard_address, "data": POLICY_SIGNER_SELECTOR}, "latest"])
        token_word = rpc.call("eth_call", [{"to": view.guard_address, "data": PAYMENT_TOKEN_SELECTOR}, "latest"])
        onchain_signer = _address_from_word(signer_word)
        onchain_token = _address_from_word(token_word)
        if signer_address and onchain_signer.lower() != signer_address.lower():
            guard_ok = False
            findings.append("FAIL  guard policySigner does not match the configured permit signing key")
        else:
            findings.append("OK    guard policySigner matches configuration")
        if onchain_token.lower() != ARC_TESTNET_USDC.lower():
            guard_ok = False
            findings.append(f"FAIL  guard paymentToken is {onchain_token}, expected {ARC_TESTNET_USDC}")
        else:
            findings.append("OK    guard paymentToken is the Arc Testnet USDC address")

        # A guard with no cap is not a guarded guard: the whole point is that the budget
        # cannot be exceeded by the caller, so verify the budget is actually set.
        per_payment = decode_uint256(rpc.call("eth_call", [{"to": view.guard_address, "data": PER_PAYMENT_CAP_SELECTOR}, "latest"]))
        epoch_cap = decode_uint256(rpc.call("eth_call", [{"to": view.guard_address, "data": EPOCH_CAP_SELECTOR}, "latest"]))
        recipient_cap = decode_uint256(
            rpc.call("eth_call", [{"to": view.guard_address, "data": RECIPIENT_EPOCH_CAP_SELECTOR}, "latest"])
        )
        epoch_length = decode_uint256(rpc.call("eth_call", [{"to": view.guard_address, "data": EPOCH_LENGTH_SELECTOR}, "latest"]))
        findings.append(
            f"INFO  guard budgets: per payment {_usdc(per_payment)} USDC, per epoch {_usdc(epoch_cap)} USDC, "
            f"per recipient {_usdc(recipient_cap)} USDC, epoch {epoch_length}s"
        )
        if per_payment == 0:
            guard_ok = False
            findings.append("FAIL  guard has no per-payment cap; the budget cannot bound a payment")
        if epoch_cap == 0:
            guard_ok = False
            findings.append("FAIL  guard has no per-epoch cap; the budget cannot bound spending over time")
        if per_payment and epoch_cap and epoch_cap < per_payment:
            guard_ok = False
            findings.append("FAIL  guard epoch cap is smaller than the per-payment cap, so no payment could ever succeed")
        if recipient_cap and epoch_cap and recipient_cap > epoch_cap:
            guard_ok = False
            findings.append("FAIL  guard per-recipient cap exceeds the epoch cap")
        if decode_bool(rpc.call("eth_call", [{"to": view.guard_address, "data": PAUSED_SELECTOR}, "latest"])):
            # A paused guard is a deliberate operating state, not a misconfiguration, but it is
            # still not ready to pay, so it must not be reported as ready.
            guard_ok = False
            findings.append("FAIL  guard is paused; payments revert until the pauser resumes it")
    else:
        guard_ok = False
        findings.append(f"TODO  no guard address is configured for the {view.label} provider; nothing is guarded yet")
    ok = ok and guard_ok

    if view.wallet_address:
        native = int(rpc.call("eth_getBalance", [view.wallet_address, "latest"]), 16)
        findings.append(
            f"INFO  {view.label} payer {view.wallet_address} holds {native} wei native (Arc pays gas in USDC, 18 decimals)"
        )
        erc20 = decode_uint256(
            rpc.call("eth_call", [{"to": ARC_TESTNET_USDC, "data": encode_balance_of(view.wallet_address)}, "latest"])
        )
        findings.append(f"INFO  {view.label} payer ERC-20 USDC balance is {_usdc(erc20)} USDC (6 decimals)")
    else:
        findings.append(f"TODO  no payer address is configured for the {view.label} provider; no balance was checked")

    if not view.configured:
        ok = False
        findings.append(f"TODO  the {view.label} provider is incomplete: {view.configuration_note}")
    if not view.live:
        ok = False
        findings.append(f"TODO  the payment provider is '{view.name}': no live payment would be sent")
    return ok, findings


def short_hash(value: str, *, full_hashes: bool = False) -> str:
    """First 10 and last 6 characters, the same shortening the console uses."""
    if full_hashes or len(value) <= 16:
        return value
    return f"{value[:10]}...{value[-6:]}"


def _line(label: str, value: str) -> str:
    """Label at the left, value at column 21, so a value reads without hunting for it."""
    return f"{label + ':':<20}{value}"


def _address_from_topic(topic: Any) -> str | None:
    """The address in one indexed address topic, or None when the word is not 32 bytes."""
    if not isinstance(topic, str) or not topic.startswith("0x") or len(topic) != 66:
        return None
    try:
        int(topic, 16)
    except ValueError:
        return None
    return "0x" + topic[-40:]


def user_operation_paymaster(receipt: dict[str, Any] | None) -> str | None:
    """Paymaster from the standard UserOperationEvent in a receipt, or None when absent.

    Only a log emitted by the Arc EntryPoint with the v0.7 event signature counts. A missing
    topic, a log from another contract and an address word of the wrong length all return None
    rather than a decoded guess.
    """
    logs = receipt.get("logs") if isinstance(receipt, dict) else None
    if not isinstance(logs, list):
        return None
    for log in logs:
        if not isinstance(log, dict) or str(log.get("address") or "").lower() != ARC_TESTNET_ENTRYPOINT:
            continue
        topics = log.get("topics")
        if not isinstance(topics, list) or len(topics) < 4:
            continue
        if str(topics[0]).lower() != USER_OPERATION_EVENT_TOPIC:
            continue
        return _address_from_topic(topics[3])
    return None


@dataclass(frozen=True)
class PaymasterCheck:
    """What one public receipt shows about the paymaster field, and nothing beyond it."""

    verdict: str
    transaction_hash: str
    event: str
    paymaster: str | None
    paymaster_note: str
    not_verified: str | None = None

    @property
    def exit_code(self) -> int:
        return {"VERIFIED": 0, "FAIL": 1, "INCONCLUSIVE": 3}[self.verdict]

    def lines(self, *, full_hashes: bool = False) -> list[str]:
        transaction = short_hash(self.transaction_hash, full_hashes=full_hashes)
        if self.verdict == "VERIFIED":
            address = short_hash(self.paymaster or "", full_hashes=full_hashes)
            headline = f"nonzero paymaster {address} in the UserOperationEvent"
            paymaster = f"{address} (protocol-level gas payer)"
        elif self.verdict == "FAIL":
            headline = "no usable paymaster in the UserOperationEvent"
            paymaster = self.paymaster_note
        else:
            headline = "receipt could not be read, nothing is verified"
            paymaster = "unverified"
        lines = [
            f"{self.verdict}  {headline}",
            _line("Network", f"Arc Testnet ({ARC_TESTNET_CHAIN_ID})"),
            _line("Transaction", transaction),
            _line("Event", self.event),
            _line("Paymaster", paymaster),
        ]
        if self.not_verified:
            lines.append(_line("Not verified", self.not_verified))
        lines.append(_line("Mode", "read-only, no credentials, no Circle API call"))
        return lines


def check_paymaster(
    rpc: RpcClient,
    transaction_hash: str,
    *,
    attempts: int = 6,
    delay_seconds: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
) -> PaymasterCheck:
    """Read one receipt and answer only whether a nonzero paymaster field is present.

    Read-only, and never a pass unless the event was read: an unreachable RPC or a missing
    receipt is INCONCLUSIVE, and a missing, malformed or zero paymaster is FAIL. The public
    endpoint sits behind several nodes and some return a null receipt for a mined transaction,
    so a null answer or an error is retried before the check gives up.
    """
    receipt: Any = None
    unreachable = False
    for attempt in range(max(1, attempts)):
        if attempt:
            sleep(delay_seconds)
        try:
            receipt = rpc.call("eth_getTransactionReceipt", [transaction_hash])
            unreachable = False
        except Exception:
            receipt = None
            unreachable = True
        if isinstance(receipt, dict):
            break
    if unreachable:
        return PaymasterCheck(
            "INCONCLUSIVE", transaction_hash, "not read (RPC unreachable)", None, "unverified"
        )
    if not isinstance(receipt, dict):
        return PaymasterCheck(
            "INCONCLUSIVE", transaction_hash, "not read (no receipt returned)", None, "unverified"
        )
    if str(receipt.get("status", "")) not in {"0x1", "1"}:
        return PaymasterCheck("FAIL", transaction_hash, "receipt status is not success", None, "unavailable")
    paymaster = user_operation_paymaster(receipt)
    if paymaster is None:
        return PaymasterCheck("FAIL", transaction_hash, "missing or malformed", None, "unavailable")
    if paymaster == ZERO_ADDRESS:
        return PaymasterCheck(
            "FAIL", transaction_hash, "UserOperationEvent found, paymaster is the zero address", None, "none (zero address)"
        )
    return PaymasterCheck(
        "VERIFIED",
        transaction_hash,
        "UserOperationEvent found, receipt status success",
        paymaster,
        "protocol-level gas payer",
        "who owns or funds the paymaster, and what the business paid",
    )


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
    parser.add_argument("--rpc-url", default=None, help="Override the configured RPC URL for this check")
    args = parser.parse_args()
    settings = get_settings()
    view = provider_view(settings)
    rpc = RpcClient(args.rpc_url or view.rpc_url)
    signer_address = (getattr(settings, "permit_signing_address", None) or "").strip() or None
    if signer_address is None and settings.permit_signing_private_key:
        from .security import EIP712PermitSigner

        signer_address = EIP712PermitSigner(settings.permit_signing_private_key).address
    try:
        ok, findings = verify(rpc, settings, signer_address=signer_address, view=view)
    except Exception as exc:  # pragma: no cover - network dependent
        print(f"FAIL  could not complete Arc verification: {type(exc).__name__}: {exc}")
        raise SystemExit(2) from exc
    print(f"Arc Testnet verification ({rpc.url})")
    print(f"Payment provider: {view.label}")
    for line in findings:
        print(f"  {line}")
    print("Result: " + ("all checks passed" if ok else "not ready for a live payment"))
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
