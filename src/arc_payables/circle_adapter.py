from __future__ import annotations

import time
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

import httpx
from eth_abi import decode

from .crypto_utils import encrypt_circle_entity_secret
from .domain import (
    ARC_TESTNET_CHAIN_ID,
    ARC_TESTNET_USDC,
    PaymentPermit,
    PaymentStatus,
    PaymentSubmission,
    ScreeningStatus,
    TreasurySnapshot,
    USDC_SCALE,
    utcnow,
)
from .evm import decode_bool, decode_uint256, encode_allowance, encode_approve, encode_balance_of, encode_permit_call, encode_used
from .ports import PaymentProvider
from .security import EIP712PermitSigner
from .settings import Settings

CIRCLE_API_BASE = "https://api.circle.com/v1/w3s"
CONTRACT_EXECUTION_PATH = "/developer/transactions/contractExecution"


class CircleAdapterError(RuntimeError):
    def __init__(self, code: str, *, uncertain: bool = False):
        self.code = code
        self.uncertain = uncertain
        super().__init__(code)


class CircleDeveloperControlledWalletProvider(PaymentProvider):
    """Circle W3S adapter constrained to a configured Arc Testnet smart wallet and guard."""

    def __init__(
        self,
        settings: Settings,
        signer: EIP712PermitSigner,
        client: httpx.Client | None = None,
        rpc_client: httpx.Client | None = None,
    ):
        self.settings = settings
        self.signer = signer
        self.api = client or httpx.Client(timeout=15.0)
        self.rpc = rpc_client or httpx.Client(timeout=12.0)
        if not settings.circle_ready:
            raise CircleAdapterError("Circle configuration is incomplete")
        # Overridable so the documented integration can be exercised against the local
        # credential-free test executor as well as Circle's real API.
        self.api_base = (getattr(settings, "circle_api_base_url", None) or CIRCLE_API_BASE).rstrip("/")
        self.guard_address = self._checksum(settings.circle_guard_address or "")
        self.wallet_address = self._checksum(settings.circle_wallet_address or "")
        self._wallet_verified = False
        self._guard_verified = False

    def get_balance(self) -> TreasurySnapshot:
        self._assert_testnet()
        self._verify_wallet()
        data = self._eth_call(ARC_TESTNET_USDC, encode_balance_of(self.wallet_address))
        balance = decode_uint256(data)
        return TreasurySnapshot(balance, utcnow(), "treasury:arc-testnet:usdc-balance", "arc_testnet_usdc_balanceOf")

    def screen_address(self, address: str | None) -> ScreeningStatus:
        # Circle's wallet transfer compliance result is transaction-scoped. No independent
        # address-screening integration is configured in this MVP; fail closed to review.
        return ScreeningStatus.UNAVAILABLE if address else ScreeningStatus.UNAVAILABLE

    def inspect_payment(self, payment: dict) -> PaymentSubmission:
        self._assert_testnet()
        self._verify_wallet()
        self._verify_guard()
        tx_id = payment.get("provider_transaction_id")
        stage = payment.get("provider_stage")
        if tx_id and stage == "guard":
            return self._circle_status(str(tx_id))
        if tx_id and stage in {"approve_reset", "approve"}:
            status = self._circle_status(str(tx_id))
            if status.status in {PaymentStatus.FAILED, PaymentStatus.UNCERTAIN, PaymentStatus.PENDING}:
                return status
            # A completed allowance operation is not the supplier payment. The provider
            # continues to the permit call using the same persisted operation keys.
        permit = PaymentPermit(**payment["permit"])
        already_used = self._guard_used(permit.payment_id)
        if already_used:
            return PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="ONCHAIN_PAYMENT_USED_HASH_REQUIRES_RECONCILIATION")
        return PaymentSubmission(PaymentStatus.NOT_FOUND)

    def submit_authorized(
        self,
        payment: dict,
        on_transaction: Callable[[str, str], None] | None = None,
    ) -> PaymentSubmission:
        self._assert_testnet()
        self._verify_wallet()
        self._verify_guard()
        permit = PaymentPermit(**payment["permit"])
        self._validate_payment(payment, permit)

        # Every attempt first checks the contract nonce. A reused Circle UUID makes an
        # uncertain Circle API retry return the same operation rather than enqueue a new one.
        if self._guard_used(permit.payment_id):
            return PaymentSubmission(PaymentStatus.UNCERTAIN, failure_code="ONCHAIN_PAYMENT_ALREADY_USED")
        balance = self.get_balance()
        if balance.balance_units < permit.amount_units:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code="INSUFFICIENT_USDC_BALANCE")
        native_balance = int(self._rpc("eth_getBalance", [self.wallet_address, "latest"]), 16)
        if native_balance <= 0:
            return PaymentSubmission(PaymentStatus.FAILED, failure_code="INSUFFICIENT_ARC_NATIVE_USDC_FOR_GAS")

        allowance = self._allowance()
        if allowance != permit.amount_units:
            if allowance > 0:
                reset = self._create_contract_execution(
                    contract=ARC_TESTNET_USDC,
                    call_data=encode_approve(self.guard_address, 0),
                    idempotency_key=payment["approve_reset_idempotency_key"],
                    payment_id=permit.payment_id,
                )
                if on_transaction:
                    on_transaction("approve_reset", reset)
                result = self._wait_transaction(reset)
                if result.status != PaymentStatus.CONFIRMED:
                    return result
            approval = self._create_contract_execution(
                contract=ARC_TESTNET_USDC,
                call_data=encode_approve(self.guard_address, permit.amount_units),
                idempotency_key=payment["approve_idempotency_key"],
                payment_id=permit.payment_id,
            )
            if on_transaction:
                on_transaction("approve", approval)
            result = self._wait_transaction(approval)
            if result.status != PaymentStatus.CONFIRMED:
                return result
            if self._allowance() != permit.amount_units:
                return PaymentSubmission(PaymentStatus.FAILED, provider_transaction_id=approval, failure_code="EXACT_ALLOWANCE_NOT_SET")

        call_data = encode_permit_call(permit, payment["signature"])
        tx_id = self._create_contract_execution(
            contract=self.guard_address,
            call_data=call_data,
            idempotency_key=payment["payment_idempotency_key"],
            payment_id=permit.payment_id,
        )
        if on_transaction:
            on_transaction("guard", tx_id)
        return self._wait_transaction(tx_id)

    def _create_contract_execution(self, contract: str, call_data: str, idempotency_key: str, payment_id: str) -> str:
        # Circle requires a fresh RSA-OAEP ciphertext for every mutating W3S request.
        ciphertext = self._fresh_entity_secret_ciphertext()
        body = {
            "idempotencyKey": idempotency_key,
            "entitySecretCiphertext": ciphertext,
            "walletId": self.settings.circle_wallet_id,
            "contractAddress": contract,
            "callData": call_data,
            "feeLevel": "MEDIUM",
            "refId": payment_id,
        }
        try:
            payload = self._api_request("POST", CONTRACT_EXECUTION_PATH, json_body=body)
        except CircleAdapterError as exc:
            if exc.uncertain:
                raise
            raise
        data = payload.get("data", {})
        tx_id = data.get("id") or (data.get("transaction") or {}).get("id")
        if not tx_id:
            raise CircleAdapterError("Circle contract call response omitted transaction ID", uncertain=True)
        return str(tx_id)

    def _wait_transaction(self, tx_id: str) -> PaymentSubmission:
        deadline = time.monotonic() + self.settings.circle_confirmation_timeout_seconds
        last_state = "UNKNOWN"
        while time.monotonic() < deadline:
            result = self._circle_status(tx_id)
            if result.status in {PaymentStatus.CONFIRMED, PaymentStatus.FAILED, PaymentStatus.UNCERTAIN}:
                return result
            last_state = result.failure_code or "PENDING"
            time.sleep(max(0.05, self.settings.circle_poll_interval_seconds))
        return PaymentSubmission(PaymentStatus.PENDING, provider_transaction_id=tx_id, failure_code=f"CONFIRMATION_PENDING:{last_state}")

    def _circle_status(self, tx_id: str) -> PaymentSubmission:
        try:
            payload = self._api_request("GET", f"/transactions/{tx_id}")
        except CircleAdapterError as exc:
            if exc.uncertain:
                return PaymentSubmission(PaymentStatus.UNCERTAIN, provider_transaction_id=tx_id, failure_code=exc.code)
            raise
        data = payload.get("data", {})
        transaction = data.get("transaction", data)
        state = str(transaction.get("state") or "UNKNOWN").upper()
        tx_hash = transaction.get("txHash") or transaction.get("transactionHash")
        fee_units = self._fee_units(transaction)
        if state == "COMPLETE":
            if not tx_hash:
                return PaymentSubmission(PaymentStatus.UNCERTAIN, provider_transaction_id=tx_id, fee_units=fee_units, failure_code="COMPLETE_WITHOUT_TX_HASH")
            return PaymentSubmission(PaymentStatus.CONFIRMED, str(tx_hash), tx_id, fee_units)
        if state in {"FAILED", "DENIED", "CANCELLED"}:
            return PaymentSubmission(PaymentStatus.FAILED, str(tx_hash) if tx_hash else None, tx_id, fee_units, f"CIRCLE_{state}")
        if state == "CONFIRMED":
            # Circle documents CONFIRMED as an intermediate state; only COMPLETE is final.
            return PaymentSubmission(PaymentStatus.PENDING, str(tx_hash) if tx_hash else None, tx_id, fee_units, "CIRCLE_CONFIRMED_NOT_COMPLETE")
        return PaymentSubmission(PaymentStatus.PENDING, str(tx_hash) if tx_hash else None, tx_id, fee_units, f"CIRCLE_{state}")

    def _fresh_entity_secret_ciphertext(self) -> str:
        try:
            data = self._api_request("GET", "/config/entity/publicKey").get("data", {})
            public_key = data.get("publicKey") or data.get("public_key")
            if not public_key:
                raise CircleAdapterError("Circle public key response is invalid")
            return encrypt_circle_entity_secret(self.settings.circle_entity_secret or "", str(public_key))
        except CircleAdapterError:
            raise
        except Exception as exc:
            raise CircleAdapterError("Could not encrypt Circle entity secret") from exc

    def _validate_payment(self, payment: dict, permit: PaymentPermit) -> None:
        if not self.signer.verify(permit, payment["signature"]):
            raise CircleAdapterError("Payment permit signature is invalid")
        expected = (
            permit.payer.lower() == self.wallet_address.lower()
            and permit.token.lower() == ARC_TESTNET_USDC.lower()
            and permit.chain_id == ARC_TESTNET_CHAIN_ID
            and permit.guard_address.lower() == self.guard_address.lower()
        )
        if not expected:
            raise CircleAdapterError("Payment permit does not match the configured Arc testnet domain")
        if not uuid.UUID(payment["payment_idempotency_key"]).version == 4:
            raise CircleAdapterError("Circle payment idempotency key must be a UUID v4")

    def _verify_wallet(self) -> None:
        if self._wallet_verified:
            return
        wallet_id = str(self.settings.circle_wallet_id)
        try:
            data = self._api_request("GET", f"/wallets/{wallet_id}").get("data", {})
        except CircleAdapterError:
            raise
        wallet = data.get("wallet", data)
        address = wallet.get("address") or wallet.get("walletAddress")
        blockchain = str(wallet.get("blockchain") or wallet.get("chain") or "").upper()
        account_type = str(wallet.get("accountType") or wallet.get("account_type") or "").upper()
        if not address or address.lower() != self.wallet_address.lower() or blockchain != "ARC-TESTNET":
            raise CircleAdapterError("Configured Circle wallet is not the expected Arc Testnet wallet")
        if account_type != "SCA":
            raise CircleAdapterError("The configured Arc wallet must be explicitly identified as a Circle SCA for the guarded execution path")
        self._wallet_verified = True

    def _verify_guard(self) -> None:
        if self._guard_verified:
            return
        signer_data = self._eth_call(self.guard_address, "0x" + self._selector("policySigner()"))
        token_data = self._eth_call(self.guard_address, "0x" + self._selector("paymentToken()"))
        onchain_signer = "0x" + signer_data[-40:]
        onchain_token = "0x" + token_data[-40:]
        if onchain_signer.lower() != self.signer.address.lower() or onchain_token.lower() != ARC_TESTNET_USDC.lower():
            raise CircleAdapterError("Guard contract signer or token does not match configured testnet policy")
        self._guard_verified = True

    def _allowance(self) -> int:
        return decode_uint256(self._eth_call(ARC_TESTNET_USDC, encode_allowance(self.wallet_address, self.guard_address)))

    def _guard_used(self, payment_id: str) -> bool:
        return decode_bool(self._eth_call(self.guard_address, encode_used(payment_id)))

    def _eth_call(self, to: str, data: str) -> str:
        result = self._rpc("eth_call", [{"to": to, "data": data}, "latest"])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise CircleAdapterError("Arc RPC returned an invalid eth_call result", uncertain=True)
        return result

    def _assert_testnet(self) -> None:
        chain_id = int(self._rpc("eth_chainId", []), 16)
        if chain_id != ARC_TESTNET_CHAIN_ID:
            raise CircleAdapterError("Configured RPC endpoint is not Arc Testnet; refusing to sign or submit")

    def _rpc(self, method: str, params: list) -> Any:
        try:
            response = self.rpc.post(self.settings.circle_rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
            response.raise_for_status()
            payload = response.json()
            if payload.get("error"):
                raise CircleAdapterError(f"Arc RPC {method} failed", uncertain=method not in {"eth_chainId"})
            return payload["result"]
        except CircleAdapterError:
            raise
        except (httpx.HTTPError, KeyError, ValueError, TypeError) as exc:
            raise CircleAdapterError(f"Arc RPC {method} unavailable", uncertain=True) from exc

    def _api_request(self, method: str, path: str, json_body: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.settings.circle_api_key}", "Accept": "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json"
        try:
            response = self.api.request(method, f"{self.api_base}{path}", headers=headers, json=json_body)
        except httpx.TimeoutException as exc:
            raise CircleAdapterError("Circle API request timed out", uncertain=method == "POST") from exc
        except httpx.HTTPError as exc:
            raise CircleAdapterError("Circle API request failed", uncertain=method == "POST") from exc
        if response.status_code >= 500:
            raise CircleAdapterError(f"Circle API returned HTTP {response.status_code}", uncertain=method == "POST")
        if response.status_code >= 400:
            raise CircleAdapterError(f"Circle API rejected the request (HTTP {response.status_code})")
        try:
            payload = response.json()
        except ValueError as exc:
            raise CircleAdapterError("Circle API returned invalid JSON", uncertain=method == "POST") from exc
        if not isinstance(payload, dict):
            raise CircleAdapterError("Circle API returned an invalid response", uncertain=method == "POST")
        return payload

    @staticmethod
    def _fee_units(transaction: dict) -> int | None:
        # Arc native USDC is 18-decimal precision, whereas ERC-20 USDC uses 6.
        # Do not infer denomination or precision from an undocumented scalar field.
        fee = transaction.get("networkFeeUsdc")
        if not isinstance(fee, dict) or fee.get("currency") != "USDC" or fee.get("decimals") != 6:
            return None
        value = fee.get("amount")
        if value is None:
            return None
        try:
            amount = Decimal(str(value))
            scaled = amount * USDC_SCALE
            if not amount.is_finite() or scaled != scaled.to_integral_value() or scaled < 0:
                return None
            return int(scaled)
        except Exception:
            return None

    @staticmethod
    def _checksum(address: str) -> str:
        if len(address) != 42 or not address.startswith("0x"):
            raise CircleAdapterError("Configured Circle wallet or guard address is invalid")
        try:
            int(address[2:], 16)
        except ValueError as exc:
            raise CircleAdapterError("Configured Circle wallet or guard address is invalid") from exc
        return address

    @staticmethod
    def _selector(signature: str) -> str:
        from eth_utils import keccak
        return keccak(text=signature)[:4].hex()
