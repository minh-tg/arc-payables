"""Arc Testnet executor that holds the payer key locally instead of at Circle.

This is the same payment path as the Circle adapter — an exact-allowance ``approve`` followed
by ``PaymentGuard.pay`` with a policy-signed permit — but the payer is an EOA whose key lives
in the environment. It exists so the flow can be exercised on Arc Testnet, and developed
against, without Circle credentials; it is not a replacement for Developer-Controlled Wallets
in production, where the key should not sit next to the backend.

What it deliberately keeps identical to the Circle path:

* Arc Testnet only, asserted from the node's ``eth_chainId`` before anything is sent.
* The guard's on-chain budget caps, which the backend cannot exceed.
* An exact allowance, so the guard can pull exactly the authorized amount and no more.
* The transaction hash is persisted before waiting for a receipt, so a crash mid-submission
  is recoverable rather than a reason to send a second payment.
* Reverts are pre-flighted with ``eth_call`` and reported with the contract's own error name
  instead of costing gas and returning a bare failure.

The fee is the measured gas cost. Arc pays gas in native USDC at 18 decimals while the ERC-20
interface is 6 decimals; they are the same balance, so the measured cost is converted to
6-decimal units and rounded up. The supplier's amount is never touched by it.
"""

from __future__ import annotations

import time
from typing import Any, Callable

import httpx
from eth_abi import encode as abi_encode
from eth_account import Account
from eth_utils import keccak, to_checksum_address

from .domain import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
    USDC_SCALE,
    PaymentPermit,
    PaymentStatus,
    PaymentSubmission,
    TreasurySnapshot,
    utcnow,
)
from .evm import decode_bool, decode_uint256, encode_allowance, encode_approve, encode_balance_of, encode_permit_call, encode_used, selector

#: Guard errors mapped to stable provider failure codes.
GUARD_ERROR_CODES = {
    "InvalidConfiguration()": "GUARD_INVALID_CONFIGURATION",
    "UnsupportedChain()": "GUARD_UNSUPPORTED_CHAIN",
    "InvalidPayer()": "GUARD_INVALID_PAYER",
    "InvalidToken()": "GUARD_INVALID_TOKEN",
    "InvalidRecipient()": "GUARD_INVALID_RECIPIENT",
    "InvalidPermit()": "GUARD_INVALID_PERMIT",
    "ExpiredPermit()": "PERMIT_EXPIRED",
    "PaymentAlreadyUsed()": "ONCHAIN_PAYMENT_ALREADY_USED",
    "InvalidSignature()": "PERMIT_SIGNATURE_REJECTED",
    "PerPaymentCapExceeded()": "GUARD_PER_PAYMENT_CAP_EXCEEDED",
    "EpochCapExceeded()": "GUARD_EPOCH_BUDGET_EXCEEDED",
    "RecipientEpochCapExceeded()": "GUARD_RECIPIENT_BUDGET_EXCEEDED",
    "TokenTransferFailed()": "GUARD_TOKEN_TRANSFER_FAILED",
}
ERROR_BY_SELECTOR = {selector(signature).hex(): code for signature, code in GUARD_ERROR_CODES.items()}
PAYMENT_EXECUTED_TOPIC = "0x" + keccak(text="PaymentExecuted(bytes32,bytes32,address,address,address,uint256)").hex()


class LocalPaymentError(RuntimeError):
    def __init__(self, message: str, *, uncertain: bool = False, code: str | None = None):
        self.uncertain = uncertain
        self.code = code
        super().__init__(message)


class LocalKeyPaymentProvider:
    """`PaymentProvider` backed by a locally held payer key on Arc Testnet."""

    def __init__(self, settings, signer=None):
        self.settings = settings
        self.signer = signer
        if not settings.local_payment_private_key:
            raise LocalPaymentError("LOCAL_PAYMENT_PRIVATE_KEY is required for the local executor")
        self._account = Account.from_key(settings.local_payment_private_key)
        self.payer_address = to_checksum_address(self._account.address)
        configured = getattr(settings, "local_payment_address", None)
        if configured and to_checksum_address(configured) != self.payer_address:
            raise LocalPaymentError("LOCAL_PAYMENT_ADDRESS does not match LOCAL_PAYMENT_PRIVATE_KEY")
        self.rpc_url = str(settings.local_payment_rpc_url).rstrip("/")
        self.guard_address = to_checksum_address(str(settings.local_payment_guard_address))
        self.token_address = to_checksum_address(ARC_TESTNET_USDC)
        self.timeout = float(getattr(settings, "local_payment_timeout_seconds", 20.0))
        self.receipt_timeout = float(getattr(settings, "local_payment_receipt_timeout_seconds", 90.0))
        self.lookback = int(getattr(settings, "local_payment_log_lookback_blocks", 20_000))
        self._client = httpx.Client(timeout=self.timeout)
        self._chain_verified = False

    @property
    def wallet_address(self) -> str:
        """Payer address, named to match the payment provider port used by the workflow."""
        return self.payer_address

    # -- JSON-RPC ------------------------------------------------------------------------

    def close(self) -> None:
        self._client.close()

    def _rpc(self, method: str, params: list, *, allow_error: bool = False) -> Any:
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        try:
            response = self._client.post(self.rpc_url, json=payload)
        except httpx.HTTPError as exc:
            raise LocalPaymentError(f"Arc RPC {method} failed at the transport level", uncertain=True) from exc
        if response.status_code >= 400:
            raise LocalPaymentError(f"Arc RPC {method} returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise LocalPaymentError(f"Arc RPC {method} returned invalid JSON", uncertain=True) from exc
        if "error" in body:
            if allow_error:
                return body["error"]
            error = body["error"] or {}
            raise LocalPaymentError(f"Arc RPC {method} rejected the request: {error.get('message', 'unknown')}")
        return body.get("result")

    def _eth_call(self, to: str, data: str) -> str:
        result = self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        if not isinstance(result, str):
            raise LocalPaymentError("eth_call returned no data")
        return result

    def assert_arc_testnet(self) -> None:
        """Fail closed unless the node really is Arc Testnet.

        Checked from the node rather than from configuration, so a mistyped RPC URL cannot
        silently send a testnet-shaped payment to another chain.
        """
        if self._chain_verified:
            return
        chain_id = int(self._rpc("eth_chainId", []), 16)
        if chain_id != ARC_TESTNET_CHAIN_ID:
            raise LocalPaymentError(f"refusing to transact: node chain id {chain_id} is not Arc Testnet")
        code = self._rpc("eth_getCode", [self.guard_address, "latest"])
        if not code or code == "0x":
            raise LocalPaymentError("no contract code at LOCAL_PAYMENT_GUARD_ADDRESS")
        self._chain_verified = True

    # -- provider interface --------------------------------------------------------------

    def get_balance(self) -> TreasurySnapshot:
        self.assert_arc_testnet()
        units = decode_uint256(self._eth_call(self.token_address, encode_balance_of(self.payer_address)))
        return TreasurySnapshot(units, utcnow(), "treasury:arc-testnet:usdc-balance", "arc_testnet_usdc_balanceOf")

    def native_gas_balance(self) -> int:
        return int(self._rpc("eth_getBalance", [self.payer_address, "latest"]), 16)

    def screen_address(self, address: str | None) -> str:  # pragma: no cover - mirrors Circle adapter
        from .domain import ScreeningStatus

        # No independent screening integration is configured here either; fail closed to review.
        return ScreeningStatus.UNAVAILABLE

    def remaining_budget(self, recipient: str) -> int | None:
        """Budget the guard still allows this epoch, read from the chain.

        Informational: the contract is the authority. Returns None when the guard predates
        the budget feature rather than guessing a number.
        """
        try:
            data = "0x" + selector("remainingEpochBudget(address)").hex() + abi_encode(["address"], [to_checksum_address(recipient)]).hex()
            return decode_uint256(self._eth_call(self.guard_address, data))
        except (LocalPaymentError, ValueError):
            # No budget view on this guard: report nothing rather than guessing a number.
            return None

    def inspect_payment(self, payment: dict) -> PaymentSubmission:
        """Resolve a payment that may or may not have landed, without sending anything."""
        self.assert_arc_testnet()
        permit = PaymentPermit(**payment["permit"])
        tx_hash = payment.get("provider_transaction_id")
        stage = payment.get("provider_stage")
        if tx_hash and stage == "guard":
            return self._status_from_receipt(str(tx_hash))
        if tx_hash and stage == "approve":
            # A completed allowance is not the supplier payment; continue below.
            status = self._status_from_receipt(str(tx_hash))
            if status.status in {PaymentStatus.FAILED, PaymentStatus.UNCERTAIN}:
                return status
        if self._guard_used(permit.payment_id):
            recovered = self._find_payment_transaction(permit.payment_id)
            if recovered:
                return PaymentSubmission(PaymentStatus.CONFIRMED, recovered)
            # The money moved but the transaction is not visible in the search window: this
            # must be reconciled by a human, never retried.
            return PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="ONCHAIN_PAYMENT_USED_HASH_REQUIRES_RECONCILIATION")
        return PaymentSubmission(PaymentStatus.NOT_FOUND)

    def submit_authorized(self, payment: dict, on_transaction: Callable[[str, str], None] | None = None) -> PaymentSubmission:
        self.assert_arc_testnet()
        permit = PaymentPermit(**payment["permit"])
        self._validate_payment(payment, permit)

        if self._guard_used(permit.payment_id):
            return PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="ONCHAIN_PAYMENT_ALREADY_USED")

        balance = self.get_balance()
        if balance.balance_units < permit.amount_units:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code="INSUFFICIENT_USDC_BALANCE")
        if self.native_gas_balance() <= 0:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code="INSUFFICIENT_ARC_NATIVE_USDC_FOR_GAS")

        # Pre-flight from on-chain state, so a payment that cannot succeed is refused without
        # spending gas and is reported with the guard's own reason. The contract remains the
        # authority; these reads only describe what it would decide.
        guard_code = self.guard_precondition_failure(permit)
        if guard_code:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code=guard_code)

        allowance = self._allowance()
        if allowance != permit.amount_units:
            if allowance > 0:
                self._send_and_wait(self.token_address, encode_approve(self.guard_address, 0), on_transaction, stage="approve")
            self._send_and_wait(self.token_address, encode_approve(self.guard_address, permit.amount_units), on_transaction, stage="approve")

        # Simulate the exact call now that the allowance is in place, so anything unforeseen is
        # caught before gas is spent on the payment itself.
        call_data = encode_permit_call(permit, str(payment["signature"]))
        revert_code = self._simulate(call_data)
        if revert_code:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code=revert_code)

        return self._send_and_wait(self.guard_address, call_data, on_transaction, stage="guard")

    def guard_precondition_failure(self, permit: PaymentPermit) -> str | None:
        """Why the guard would refuse this permit, read from the chain; or None.

        Each check mirrors something the contract enforces. None of them replace it: the
        on-chain call is still the decision, and this only avoids paying gas to learn it.
        """
        if self._guard_paused():
            return "GUARD_PAUSED"
        if self._guard_used(permit.payment_id):
            return "ONCHAIN_PAYMENT_ALREADY_USED"
        if self._read_address("policySigner()").lower() != str(getattr(self.signer, "address", "")).lower():
            return "GUARD_POLICY_SIGNER_MISMATCH"
        if self._read_address("paymentToken()").lower() != self.token_address.lower():
            return "GUARD_TOKEN_MISMATCH"
        latest = self._rpc("eth_getBlockByNumber", ["latest", False]) or {}
        if int(latest.get("timestamp", "0x0"), 16) >= permit.expiry:
            return "PERMIT_EXPIRED"
        cap = self._read_uint("perPaymentCap()")
        if cap and permit.amount_units > cap:
            return "GUARD_PER_PAYMENT_CAP_EXCEEDED"
        remaining = self.remaining_budget(permit.recipient)
        if remaining is not None and permit.amount_units > remaining:
            return "GUARD_EPOCH_BUDGET_EXCEEDED"
        return None

    def _guard_paused(self) -> bool:
        return decode_bool(self._eth_call(self.guard_address, "0x" + selector("paused()").hex()))

    def guard_limits(self) -> dict[str, int] | None:
        """The deployed guard's budgets, read from the chain; None if it cannot be read.

        Exposed publicly so a preflight can refuse a payment the contract would reject, rather
        than spending gas to discover it.
        """
        try:
            return {
                "per_payment_cap": self._read_uint("perPaymentCap()"),
                "epoch_cap": self._read_uint("epochCap()"),
                "recipient_epoch_cap": self._read_uint("recipientEpochCap()"),
                "epoch_length": self._read_uint("epochLength()"),
                "paused": 1 if self._guard_paused() else 0,
            }
        except LocalPaymentError:
            return None

    def _read_address(self, signature: str) -> str:
        result = self._eth_call(self.guard_address, "0x" + selector(signature).hex())
        return to_checksum_address("0x" + result[-40:])

    def _read_uint(self, signature: str) -> int:
        return decode_uint256(self._eth_call(self.guard_address, "0x" + selector(signature).hex()))

    # -- internals -----------------------------------------------------------------------

    def _validate_payment(self, payment: dict, permit: PaymentPermit) -> None:
        if permit.payer.lower() != self.payer_address.lower():
            raise LocalPaymentError("permit payer is not this executor's address")
        if permit.token.lower() != self.token_address.lower():
            raise LocalPaymentError("permit token is not Arc Testnet USDC")
        if permit.guard_address.lower() != self.guard_address.lower():
            raise LocalPaymentError("permit guard address is not the configured guard")
        if permit.chain_id != ARC_TESTNET_CHAIN_ID:
            raise LocalPaymentError("permit chain id is not Arc Testnet")
        if not payment.get("signature"):
            raise LocalPaymentError("payment is missing the policy signature")

    def _simulate(self, call_data: str) -> str | None:
        error = self._rpc(
            "eth_call",
            [{"from": self.payer_address, "to": self.guard_address, "data": call_data}, "latest"],
            allow_error=True,
        )
        if not isinstance(error, dict):
            return None
        data = str(error.get("data") or "")
        message = str(error.get("message") or "")
        for candidate in (data, message):
            selector_hex = candidate[2:10].lower() if candidate.startswith("0x") and len(candidate) >= 10 else ""
            if selector_hex in ERROR_BY_SELECTOR:
                return ERROR_BY_SELECTOR[selector_hex]
        # A plain revert, typically a token allowance/balance failure from transferFrom.
        if "revert" in message.lower() or data:
            return "GUARD_CALL_REVERTED"
        return None

    def _allowance(self) -> int:
        data = encode_allowance(self.payer_address, self.guard_address)
        return decode_uint256(self._eth_call(self.token_address, data))

    def _guard_used(self, payment_id: str) -> bool:
        return decode_bool(self._eth_call(self.guard_address, encode_used(payment_id)))

    def _fee_fields(self) -> dict:
        latest = self._rpc("eth_getBlockByNumber", ["latest", False]) or {}
        base_fee = int(latest.get("baseFeePerGas") or "0x0", 16)
        if base_fee > 0:
            tip = 10**9
            return {"maxFeePerGas": base_fee * 2 + tip, "maxPriorityFeePerGas": tip}
        gas_price = int(self._rpc("eth_gasPrice", []), 16)
        return {"gasPrice": gas_price}

    def _nonce(self) -> int:
        return int(self._rpc("eth_getTransactionCount", [self.payer_address, "pending"]), 16)

    def _send_and_wait(self, to: str, data: str, on_transaction, *, stage: str) -> PaymentSubmission:
        try:
            estimated = int(self._rpc("eth_estimateGas", [{"from": self.payer_address, "to": to, "data": data}]), 16)
            gas = estimated * 2
        except LocalPaymentError:
            # Estimating a call that we expect to succeed should not fail; if it does, the
            # call is attempted with a generous limit and any revert is reported honestly.
            gas = 500_000
        transaction = {
            "to": to,
            "data": data,
            "value": 0,
            "chainId": ARC_TESTNET_CHAIN_ID,
            "nonce": self._nonce(),
            "gas": gas,
            **self._fee_fields(),
        }
        signed = self._account.sign_transaction(transaction)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        tx_hash = str(self._rpc("eth_sendRawTransaction", ["0x" + raw.hex()]))
        # Persist the hash before waiting: a crash from here on is recoverable, and a second
        # submission is never the answer.
        if on_transaction is not None:
            on_transaction(stage, tx_hash)
        return self._status_from_receipt(tx_hash)

    def _status_from_receipt(self, tx_hash: str) -> PaymentSubmission:
        receipt = self._wait_receipt(tx_hash)
        if receipt is None:
            return PaymentSubmission(PaymentStatus.UNCERTAIN, tx_hash, failure_code="RECEIPT_NOT_OBSERVED")
        if int(receipt.get("status", "0x0"), 16) != 1:
            return PaymentSubmission(PaymentStatus.FAILED, tx_hash, failure_code="TRANSACTION_REVERTED")
        gas_used = int(receipt.get("gasUsed", "0x0"), 16)
        price = int(receipt.get("effectiveGasPrice") or receipt.get("gasPrice") or "0x0", 16)
        return PaymentSubmission(PaymentStatus.CONFIRMED, tx_hash, fee_units=self._fee_units(gas_used * price))

    @staticmethod
    def _fee_units(wei: int) -> int:
        """Native gas cost (18-decimal USDC) as 6-decimal USDC units, rounded up.

        Rounding up can only overstate our own expense; it never reduces the supplier's
        amount, and the ERPNext entry stays balanced because the same figure is used for both
        the outflow and the deduction.
        """
        if wei <= 0:
            return 0
        return -(-wei // (USDC_SCALE * 10**12))

    def _wait_receipt(self, tx_hash: str) -> dict | None:
        deadline = time.monotonic() + self.receipt_timeout
        while time.monotonic() < deadline:
            receipt = self._rpc("eth_getTransactionReceipt", [tx_hash])
            if receipt:
                return receipt
            time.sleep(0.5)
        return None

    def _find_payment_transaction(self, payment_id: str) -> str | None:
        latest = int(self._rpc("eth_blockNumber", []), 16)
        from_block = max(0, latest - self.lookback)
        logs = self._rpc(
            "eth_getLogs",
            [
                {
                    "address": self.guard_address,
                    "topics": [PAYMENT_EXECUTED_TOPIC, "0x" + payment_id.removeprefix("0x")],
                    "fromBlock": hex(from_block),
                    "toBlock": "latest",
                }
            ],
        ) or []
        return str(logs[0]["transactionHash"]) if logs else None
