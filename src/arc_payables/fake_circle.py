"""A credential-free Circle + Arc test executor.

This drives the **real** payment adapter, the **real** guard contract and a **real** EVM, with
no Circle account, no funded wallet and no network access beyond localhost:

* ``AnvilChain`` runs ``anvil`` with chain id 5042002 (Arc Testnet's id), deploys the real
  ``PaymentGuard`` bytecode and a 6-decimal USDC ERC-20, and signs/sends raw transactions.
* ``FakeCircleApi`` speaks the Circle Developer-Controlled Wallets endpoints the adapter uses.
  It decrypts the RSA-OAEP entity-secret ciphertext with its own key, so a broken entity-secret
  flow fails here rather than silently passing a shape check. It honours Circle's idempotency
  key semantics and reports an explicit ``networkFeeUsdc`` amount.

Nothing here is production infrastructure and nothing is deployed to a public network. On the
local EVM gas is paid in the EVM's native currency; on Arc it is paid in native USDC. The
adapter only requires that a native balance exists to pay gas, which is what this satisfies.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA
from eth_account import Account
from eth_utils import to_checksum_address

from .crypto_utils import encrypt_circle_entity_secret
from .domain import ARC_TESTNET_CHAIN_ID, ARC_TESTNET_USDC, USDC_SCALE
from .evm import encode_used, selector

REPO_ROOT = Path(__file__).resolve().parents[2]

# Budget caps for the locally deployed guard. Deliberately finite so a local run exercises the
# same on-chain limits the real deployment enforces; a test can pass tighter values.
DEMO_PER_PAYMENT_CAP = 1_000 * USDC_SCALE          # 1,000 USDC in a single payment
DEMO_EPOCH_CAP = 10_000 * USDC_SCALE               # 10,000 USDC per epoch across all suppliers
DEMO_RECIPIENT_EPOCH_CAP = 5_000 * USDC_SCALE      # 5,000 USDC per supplier per epoch
DEMO_EPOCH_LENGTH = 86_400                         # one day
EPOCH_SPENT_SELECTOR = "0x" + selector("epochSpent(uint64)").hex()
ARTIFACT_DIR = REPO_ROOT / "out"
DEFAULT_WALLET_KEY = "0x" + "5c" * 32
DEFAULT_ENTITY_SECRET = "ab" * 32
DEFAULT_POLICY_KEY = "0x" + "11" * 32
DEFAULT_FEE_UNITS = 10_000  # 0.01 USDC


class FakeExecutorError(RuntimeError):
    pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def load_artifact(name: str) -> dict:
    path = ARTIFACT_DIR / f"{name}.sol" / f"{name}.json"
    if not path.exists():
        raise FakeExecutorError(f"Missing Foundry artifact {path}. Run `forge build` first.")
    return json.loads(path.read_text())


class AnvilChain:
    """A local EVM with Arc Testnet's chain id, exposing just what the executor needs."""

    def __init__(self, port: int | None = None, *, binary: str = "anvil", chain_id: int = ARC_TESTNET_CHAIN_ID):
        self.port = port or _free_port()
        self.chain_id = chain_id
        self.binary = binary
        self.process: subprocess.Popen | None = None
        self.url = f"http://127.0.0.1:{self.port}"
        self._client = httpx.Client(timeout=20.0)
        self._request_id = 0
        self.deployer = Account.from_key(DEFAULT_WALLET_KEY)
        self.wallet = self.deployer  # the "Circle SCA" stand-in
        self.token_address: str | None = None
        self.guard_address: str | None = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self, timeout: float = 30.0) -> "AnvilChain":
        if shutil.which(self.binary) is None:
            raise FakeExecutorError(f"{self.binary} is not installed; install Foundry to run the fake executor")
        self.process = subprocess.Popen(
            [
                self.binary,
                "--chain-id",
                str(self.chain_id),
                "--port",
                str(self.port),
                "--silent",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                detail = (self.process.stderr.read() if self.process.stderr else "") or ""
                raise FakeExecutorError(f"anvil exited during startup: {detail.strip()[:400]}")
            try:
                chain_id = int(self.rpc("eth_chainId", []), 16)
                if chain_id == self.chain_id:
                    # anvil only pre-funds its own default accounts, so the executor's wallet
                    # (the Circle SCA stand-in) needs native currency to pay gas.
                    self.fund_native(self.wallet.address, 10**19)
                    return self
            except Exception:
                time.sleep(0.2)
        self.stop()
        raise FakeExecutorError("anvil did not become ready")

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover - defensive
                self.process.kill()
        self.process = None

    def __enter__(self) -> "AnvilChain":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def __del__(self) -> None:
        """Safety net for runs that never reach teardown.

        Normal use goes through the context manager. This covers the cases where teardown is
        skipped entirely - a fixture erroring during setup, or the test process being killed -
        so an abandoned chain cannot leave an anvil process holding a port.
        """
        try:
            self.stop()
        except Exception:  # pragma: no cover - defensive, never raise from a finalizer
            pass

    # -- JSON-RPC ----------------------------------------------------------------------
    def rpc(self, method: str, params: list) -> Any:
        self._request_id += 1
        response = self._client.post(
            self.url, json={"jsonrpc": "2.0", "id": self._request_id, "method": method, "params": params}
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("error"):
            raise FakeExecutorError(f"RPC {method} failed: {payload['error'].get('message')}")
        return payload["result"]

    def call(self, to: str, data: str) -> str:
        return self.rpc("eth_call", [{"to": to, "data": data}, "latest"])

    def fund_native(self, address: str, wei: int) -> None:
        self.rpc("anvil_setBalance", [address, hex(wei)])

    def native_balance(self, address: str) -> int:
        return int(self.rpc("eth_getBalance", [address, "latest"]), 16)

    def send(self, *, to: str | None, data: str, private_key=None) -> str:
        account = Account.from_key(private_key) if private_key else self.deployer
        tx = {
            "chainId": self.chain_id,
            "nonce": int(self.rpc("eth_getTransactionCount", [account.address, "pending"]), 16),
            "gasPrice": int(self.rpc("eth_gasPrice", []), 16),
            "gas": 3_000_000,
            # eth_account requires checksummed addresses; receipts return lowercase ones.
            "to": to_checksum_address(to) if to else None,
            "value": 0,
            "data": data,
        }
        signed = Account.sign_transaction(tx, account.key)
        return self.rpc("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()])

    def receipt(self, tx_hash: str) -> dict | None:
        return self.rpc("eth_getTransactionReceipt", [tx_hash])

    def wait_receipt(self, tx_hash: str, timeout: float = 20.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            found = self.receipt(tx_hash)
            if found:
                return found
            time.sleep(0.1)
        raise FakeExecutorError(f"transaction {tx_hash} was not mined")

    def deploy(self, artifact: dict, *args: str) -> str:
        constructor = _encode_constructor(artifact, args)
        tx_hash = self.send(to=None, data=artifact["bytecode"]["object"] + constructor)
        receipt = self.wait_receipt(tx_hash)
        if not receipt.get("contractAddress"):
            raise FakeExecutorError(f"deployment failed: {receipt}")
        return to_checksum_address(receipt["contractAddress"])

    def deploy_suite(
        self,
        policy_signer: str,
        *,
        token: str = ARC_TESTNET_USDC,
        per_payment_cap: int = DEMO_PER_PAYMENT_CAP,
        epoch_cap: int = DEMO_EPOCH_CAP,
        recipient_epoch_cap: int = DEMO_RECIPIENT_EPOCH_CAP,
        epoch_length: int = DEMO_EPOCH_LENGTH,
        pauser: str | None = None,
    ) -> tuple[str, str]:
        """Present a 6-decimal USDC at Arc's documented address, then deploy the real guard.

        The adapter deliberately targets the hardcoded Arc Testnet USDC address, so the local
        EVM exposes the test token at that exact address instead of the test bypassing it.

        The guard's budget caps are passed explicitly, so a local run exercises the same
        on-chain limits the deployed guard enforces. Amounts are 6-decimal USDC units.
        """
        implementation = self.deploy(load_artifact("TestArcUSDC"))
        runtime_code = self.rpc("eth_getCode", [implementation, "latest"])
        if not runtime_code or runtime_code == "0x":
            raise FakeExecutorError("deployed test token has no runtime code")
        self.set_code(token, runtime_code)
        self.token_address = to_checksum_address(token)
        self.guard_address = self.deploy(
            load_artifact("PaymentGuard"),
            self.token_address,
            policy_signer,
            pauser or self.wallet.address,
            per_payment_cap,
            epoch_cap,
            recipient_epoch_cap,
            epoch_length,
        )
        return self.token_address, self.guard_address

    def set_guard_paused(self, paused: bool) -> str:
        """Call the guard's pause control as the pauser, so a local run can exercise it."""
        signature = "pause()" if paused else "unpause()"
        data = "0x" + selector(signature).hex()
        return self.wait_receipt(self.send(to=self.guard_address, data=data))["transactionHash"]

    def guard_paused(self) -> bool:
        data = "0x" + selector("paused()").hex()
        word = self.rpc("eth_call", [{"to": self.guard_address, "data": data}, "latest"])
        return int(word, 16) == 1

    def epoch_spent(self, epoch: int = 0) -> int:
        """Budget consumed in an epoch, read from the deployed guard."""
        word = self.rpc("eth_call", [{"to": self.guard_address, "data": EPOCH_SPENT_SELECTOR + _word(hex(epoch))}, "latest"])
        return int(word, 16)

    def set_code(self, address: str, code: str) -> None:
        self.rpc("anvil_setCode", [to_checksum_address(address), code])

    def mint(self, to: str, units: int) -> str:
        data = "0x40c10f19" + _word(to) + _word(hex(units))
        return self.wait_receipt(self.send(to=self.token_address, data=data))["transactionHash"]

    def transfer(self, to: str, units: int, token: str | None = None) -> str:
        data = "0xa9059cbb" + _word(to) + _word(hex(units))
        return self.wait_receipt(self.send(to=token or self.token_address, data=data))["transactionHash"]

    def erc20_balance(self, owner: str, token: str | None = None) -> int:
        token = token or self.token_address
        result = self.call(token, "0x70a08231" + _word(owner))
        return int(result, 16)

    def erc20_allowance(self, owner: str, spender: str, token: str | None = None) -> int:
        token = token or self.token_address
        return int(self.call(token, "0xdd62ed3e" + _word(owner) + _word(spender)), 16)

    def guard_used(self, payment_id: str, guard: str | None = None) -> bool:
        guard = guard or self.guard_address
        return int(self.call(guard, encode_used(payment_id)), 16) == 1


def _word(value: str) -> str:
    if value.startswith("0x"):
        if len(value) == 42:
            return value[2:].lower().rjust(64, "0")
        return value.removeprefix("0x").rjust(64, "0")
    return hex(int(value)).removeprefix("0x").rjust(64, "0")


def _encode_constructor(artifact: dict, args: tuple[str, ...]) -> str:
    from eth_abi import encode as abi_encode

    types = [item["type"] for item in artifact["abi"] if item.get("type") == "constructor" for item in item["inputs"]]
    if not types:
        return ""
    return abi_encode(types, list(args)).hex()


@dataclass
class FakeCircleState:
    """Observable state of the fake Circle service, used by tests and the CLI."""

    wallet_id: str = "fake-circle-wallet-0001"
    entity_secret: str = DEFAULT_ENTITY_SECRET
    fee_units: int = DEFAULT_FEE_UNITS
    expected_blockchain: str = "ARC-TESTNET"
    account_type: str = "SCA"
    entity_secret_sightings: list[str] = field(default_factory=list)
    idempotency_keys: list[str] = field(default_factory=list)
    submitted_calls: list[dict] = field(default_factory=list)
    transactions: dict[str, dict] = field(default_factory=dict)
    fail_first_submission_with: int | None = None
    lose_first_submission_response: bool = False
    _lost_once: bool = False
    _failed_once: bool = False


class FakeCircleApi:
    """Circle Developer-Controlled Wallets emulator over the documented endpoints."""

    def __init__(self, chain: AnvilChain, state: FakeCircleState | None = None):
        self.chain = chain
        self.state = state or FakeCircleState()
        self.rsa_key = RSA.generate(2048)
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}/v1/w3s"
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle ---------------------------------------------------------------------
    def start(self) -> "FakeCircleApi":
        api = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):  # keep test output clean
                pass

            def _send(self, status: int, body: dict) -> None:
                payload = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):  # noqa: N802
                path = self.path.split("?")[0]
                if path.endswith("/config/entity/publicKey"):
                    return self._send(200, {"data": {"publicKey": api.rsa_key.publickey().export_key().decode()}})
                if "/wallets/" in path:
                    wallet_id = path.rsplit("/", 1)[-1]
                    return self._send(200, {"data": {"wallet": {
                        "id": wallet_id,
                        "address": api.chain.wallet.address,
                        "blockchain": api.state.expected_blockchain,
                        "accountType": api.state.account_type,
                    }}})
                if "/transactions/" in path:
                    tx_id = path.rsplit("/", 1)[-1]
                    return self._send(200, api.transaction_payload(tx_id))
                return self._send(404, {"error": "not found"})

            def do_POST(self):  # noqa: N802
                path = self.path.split("?")[0]
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                if not path.endswith("/developer/transactions/contractExecution"):
                    return self._send(404, {"error": "not found"})
                api.state.idempotency_keys.append(str(body.get("idempotencyKey")))
                plaintext = api.decrypt_entity_secret(str(body.get("entitySecretCiphertext") or ""))
                if plaintext is None:
                    return self._send(400, {"error": "entity secret ciphertext could not be decrypted"})
                api.state.entity_secret_sightings.append(plaintext)
                existing = api.state.transactions.get(str(body.get("idempotencyKey")))
                if existing:
                    # Circle returns the original operation for a repeated idempotency key.
                    return self._send(200, {"data": {"id": existing["id"]}})
                try:
                    record = api.execute(body)
                except FakeExecutorError as exc:
                    return self._send(400, {"error": str(exc)})
                api.state.transactions[str(body.get("idempotencyKey"))] = record
                if api.state.fail_first_submission_with and not api.state._failed_once:
                    api.state._failed_once = True
                    return self._send(api.state.fail_first_submission_with, {"error": "simulated failure"})
                if api.state.lose_first_submission_response and not api.state._lost_once:
                    # The transaction was broadcast but the response never reached the caller.
                    api.state._lost_once = True
                    self.close_connection = True
                    return
                return self._send(200, {"data": {"id": record["id"]}})

        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()
        self._server = None

    def __enter__(self) -> "FakeCircleApi":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # -- behaviour ---------------------------------------------------------------------
    def decrypt_entity_secret(self, ciphertext: str) -> str | None:
        try:
            raw = base64.b64decode(ciphertext)
            plaintext = PKCS1_OAEP.new(self.rsa_key, hashAlgo=SHA256).decrypt(raw)
        except Exception:
            return None
        return plaintext.hex()

    def execute(self, body: dict) -> dict:
        if str(body.get("walletId")) != self.state.wallet_id:
            raise FakeExecutorError("unknown wallet id")
        call_data = str(body.get("callData") or "")
        contract = str(body.get("contractAddress") or "")
        if not call_data.startswith("0x") or len(call_data) < 10:
            raise FakeExecutorError("invalid call data")
        tx_hash = self.chain.send(to=contract, data=call_data)
        receipt = self.chain.wait_receipt(tx_hash)
        record = {
            "id": "fake-tx-" + hashlib.sha256(tx_hash.encode()).hexdigest()[:20],
            "txHash": tx_hash,
            "contract": contract,
            "callData": call_data,
            "status": int(receipt.get("status", "0x1"), 16),
            "blockNumber": int(receipt.get("blockNumber", "0x0"), 16),
            "refId": body.get("refId"),
        }
        self.state.submitted_calls.append(
            {"contract": contract, "callData": call_data, "refId": body.get("refId"), "txHash": tx_hash}
        )
        return record

    def transaction_payload(self, tx_id: str) -> dict:
        record = next((item for item in self.state.transactions.values() if item["id"] == tx_id), None)
        if record is None:
            return {"data": {"transaction": {"id": tx_id, "state": "FAILED", "errorReason": "NOT_FOUND"}}}
        if record["status"] != 1:
            return {"data": {"transaction": {"id": tx_id, "state": "FAILED", "txHash": record["txHash"]}}}
        return {
            "data": {
                "transaction": {
                    "id": tx_id,
                    "state": "COMPLETE",
                    "txHash": record["txHash"],
                    # Explicit denomination and precision, as the adapter requires for ERPNext.
                    "networkFeeUsdc": {
                        "currency": "USDC",
                        "decimals": 6,
                        "amount": f"{self.state.fee_units / USDC_SCALE:f}",
                    },
                }
            }
        }


def main() -> None:
    """CLI: run the fake Circle API against a local Arc-chain-id EVM and print the settings."""
    import argparse

    parser = argparse.ArgumentParser(description="Run a credential-free Circle/Arc test executor")
    parser.add_argument("--anvil-port", type=int, default=None)
    parser.add_argument("--api-port", type=int, default=None)
    parser.add_argument("--usdc", type=float, default=5_000.0, help="Test USDC to mint to the fake SCA")
    args = parser.parse_args()

    chain = AnvilChain(port=args.anvil_port).start()
    try:
        policy_address = Account.from_key(os.environ.get("PERMIT_SIGNING_PRIVATE_KEY") or DEFAULT_POLICY_KEY).address
        token, guard = chain.deploy_suite(policy_address)
        chain.fund_native(chain.wallet.address, 10**18)
        chain.mint(chain.wallet.address, int(args.usdc * USDC_SCALE))
        api = FakeCircleApi(chain)
        if args.api_port:
            api.port = args.api_port
            api.base_url = f"http://127.0.0.1:{api.port}/v1/w3s"
        api.start()
        print("Fake Circle/Arc test executor is running. Nothing here is a real network.")
        print(f"  EVM RPC            : {chain.url}  (chain id {chain.chain_id})")
        print(f"  Circle API base    : {api.base_url}")
        print(f"  Fake SCA wallet    : {api.state.wallet_id}")
        print(f"  Fake SCA address   : {chain.wallet.address}")
        print(f"  Test USDC token    : {token}")
        print(f"  PaymentGuard       : {guard}")
        print(f"  ERC-20 balance     : {chain.erc20_balance(chain.wallet.address) / USDC_SCALE:,.2f} USDC")
        print("")
        print("Point a local backend at it with:")
        print("  PAYMENT_PROVIDER=circle")
        print(f"  CIRCLE_API_KEY=fake-circle-key")
        print(f"  CIRCLE_ENTITY_SECRET={DEFAULT_ENTITY_SECRET}")
        print(f"  CIRCLE_WALLET_ID={api.state.wallet_id}")
        print(f"  CIRCLE_WALLET_ADDRESS={chain.wallet.address}")
        print(f"  CIRCLE_GUARD_ADDRESS={guard}")
        print(f"  CIRCLE_RPC_URL={chain.url}")
        print(f"  CIRCLE_API_BASE_URL={api.base_url}")
        print(f"  PERMIT_SIGNING_PRIVATE_KEY={os.environ.get('PERMIT_SIGNING_PRIVATE_KEY') or DEFAULT_POLICY_KEY}")
        print("")
        print("Press Ctrl-C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nStopping.")
    finally:
        chain.stop()


if __name__ == "__main__":
    main()
