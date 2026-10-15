"""Verify and publish the head of the global audit chain on Arc Testnet.

The audit chain already detects an edit inside one database. This tool covers the case the
database cannot: someone who rewrites the log and recomputes its hashes so it verifies again.
The head at a published entry count is recorded in a public contract, so a recomputed log no
longer matches what was anchored at that count.

`verify` reads the latest published anchor and recomputes the local head at the anchored entry
count. `publish` verifies the local chain first and then, only with an explicit `--send`, signs
and sends one transaction. The signing key is read from `AUDIT_ANCHOR_PRIVATE_KEY` and is never
printed, passed on the command line, or written to an error message.

    uv run arc-payables-anchor verify
    uv run arc-payables-anchor publish            # dry run, sends nothing
    uv run arc-payables-anchor publish --send     # one transaction, spends gas

Exit codes: 0 the anchor matches or the dry run succeeded, 1 a verified mismatch or a broken
local chain, 2 missing configuration or a refusal, 3 the published anchor could not be read.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import keccak

from .domain import ARC_TESTNET_CHAIN_ID
from .evm import selector
from .store import GENESIS_HASH, SQLiteEvidenceStore
from .verify_arc import RpcClient, short_hash

ANCHORED_EVENT_SIGNATURE = "Anchored(bytes32,bytes32,uint64,uint32,uint256,address)"
ANCHORED_EVENT_TOPIC = "0x" + keccak(text=ANCHORED_EVENT_SIGNATURE).hex()
#: The versioned application identifier the contract writes into every Anchored event. Kept here so
#: the verifier rejects a head anchored for another application instead of comparing it.
ANCHOR_APPLICATION_ID = "0x" + keccak(text="arc-payables/audit-anchor/v1").hex()
PUBLISH_SELECTOR = "0x" + selector("publish(bytes32,uint64,uint32)").hex()
DEFAULT_ANCHOR_RPC_URL = "https://rpc.testnet.arc.io"
DEFAULT_ANCHOR_VERSION = 1
MINIMUM_GAS = 80_000

CHAIN_REASON_PHRASES = {
    "prev_hash_does_not_match_the_previous_entry": "chain link mismatch",
    "entry_contents_do_not_match_its_hash": "hash mismatch",
    "signature_is_not_from_the_configured_signer": "signature mismatch",
    "recorded_signer_is_not_the_configured_signer": "signer mismatch",
}


class AnchorConfigurationError(ValueError):
    """The environment does not describe a usable anchor check. Never carries a secret."""


@dataclass(frozen=True)
class AnchorConfig:
    rpc_url: str
    address: str
    version: int
    signer: str | None = None


@dataclass(frozen=True)
class AnchorEvent:
    """One Anchored event as read from the contract's logs."""

    application_id: str
    head: str
    entries: int
    version: int
    chain_id: int
    publisher: str
    block_number: int


@dataclass(frozen=True)
class AnchorOutcome:
    """A terminal verdict and the exact lines to print for it."""

    verdict: str
    exit_code: int
    lines: list[str]


def _line(label: str, value: str) -> str:
    return f"{label + ':':<20}{value}"


def _int_hex(value: Any, default: int = 0) -> int:
    try:
        return int(str(value), 16)
    except (TypeError, ValueError):
        return default


def _is_address(value: str) -> bool:
    if not value.startswith("0x") or len(value) != 42:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def config_from_environment(environment: Mapping[str, str] | None = None) -> AnchorConfig:
    """Read the anchor endpoint from the environment. Never reads or reports a key."""
    values = os.environ if environment is None else environment
    address = (values.get("AUDIT_ANCHOR_ADDRESS") or "").strip()
    if not address:
        raise AnchorConfigurationError("AUDIT_ANCHOR_ADDRESS is not set.")
    if not _is_address(address):
        raise AnchorConfigurationError("AUDIT_ANCHOR_ADDRESS is not a 20-byte address.")
    rpc_url = (values.get("AUDIT_ANCHOR_RPC_URL") or DEFAULT_ANCHOR_RPC_URL).strip()
    raw_version = (values.get("AUDIT_ANCHOR_VERSION") or str(DEFAULT_ANCHOR_VERSION)).strip()
    try:
        version = int(raw_version)
    except ValueError as exc:
        raise AnchorConfigurationError("AUDIT_ANCHOR_VERSION must be an integer.") from exc
    if not 0 <= version <= 0xFFFFFFFF:
        raise AnchorConfigurationError("AUDIT_ANCHOR_VERSION is outside uint32.")
    signer = (values.get("AUDIT_ANCHOR_SIGNER") or "").strip() or None
    if signer is not None and not _is_address(signer):
        raise AnchorConfigurationError("AUDIT_ANCHOR_SIGNER is not a 20-byte address.")
    return AnchorConfig(rpc_url=rpc_url, address=address, version=version, signer=signer)


def chained_rows(store: SQLiteEvidenceStore) -> list[dict[str, Any]]:
    """The chained audit rows in global id order, the rows `verify_audit_chain` checks."""
    with store._connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                "SELECT id,invoice_id,event_type,state,payload_json,created_at,prev_hash,event_hash,signature,signer "
                "FROM audit_events WHERE event_hash IS NOT NULL ORDER BY id"
            ).fetchall()
        ]


def head_at_entry(store: SQLiteEvidenceStore, entries: int) -> str | None:
    """The head after the first `entries` chained entries, or None when the store holds fewer.

    The prefix is verified with the store's own link and hash check, so this walks the chain
    rather than hashing it a second way.
    """
    if entries < 1:
        return None
    head = GENESIS_HASH
    for index, row in enumerate(chained_rows(store), start=1):
        if SQLiteEvidenceStore._chain_problem(row, head):
            return None
        head = row["event_hash"]
        if index == entries:
            return head
    return None


def _signature_phrase(report: Mapping[str, Any]) -> str:
    if not report.get("signed"):
        return "no signatures"
    return "signatures present" if report.get("signature_anchor") == "configured_signer" else "signatures unanchored"


def _local_summary(report: Mapping[str, Any]) -> str:
    return f"verified, {report.get('checked', 0)} entries, {_signature_phrase(report)}"


def _reason_phrase(reason: str | None) -> str:
    return CHAIN_REASON_PHRASES.get(reason or "", reason or "unknown reason")


def _decode_anchor_log(log: dict[str, Any]) -> AnchorEvent | None:
    topics = log.get("topics")
    data = log.get("data")
    if not isinstance(topics, list) or len(topics) < 4:
        return None
    if str(topics[0]).lower() != ANCHORED_EVENT_TOPIC:
        return None
    try:
        entries, version, chain_id = decode(
            ["uint64", "uint32", "uint256"], bytes.fromhex(str(data).removeprefix("0x"))
        )
    except Exception:
        return None
    application_id = "0x" + str(topics[1])[-64:]
    head = "0x" + str(topics[2])[-64:]
    publisher = "0x" + str(topics[3])[-40:]
    return AnchorEvent(
        application_id,
        head,
        int(entries),
        int(version),
        int(chain_id),
        publisher,
        _int_hex(log.get("blockNumber")),
    )


def read_latest_anchor(rpc: RpcClient, address: str) -> AnchorEvent | None:
    """The most recent Anchored event, or None when the contract has published none.

    Raises whatever the RPC raises; the caller decides that an unreadable anchor is
    inconclusive rather than a match.
    """
    logs = rpc.call(
        "eth_getLogs",
        [{"address": address, "topics": [ANCHORED_EVENT_TOPIC], "fromBlock": "0x0", "toBlock": "latest"}],
    )
    if not isinstance(logs, list):
        return None
    decoded = []
    for log in logs:
        if not isinstance(log, dict):
            continue
        event = _decode_anchor_log(log)
        if event is None:
            continue
        decoded.append((_int_hex(log.get("blockNumber")), _int_hex(log.get("logIndex")), event))
    if not decoded:
        return None
    return max(decoded, key=lambda item: (item[0], item[1]))[2]


def _network_label(chain_id: int) -> str:
    if chain_id == ARC_TESTNET_CHAIN_ID:
        return f"Arc Testnet ({ARC_TESTNET_CHAIN_ID})"
    return f"chain {chain_id}"


def _unreadable(network: str, local: str, reason: str) -> AnchorOutcome:
    return AnchorOutcome(
        "INCONCLUSIVE",
        3,
        [
            "INCONCLUSIVE  published head could not be read, nothing is compared",
            _line("Network", network),
            _line("Local chain", local),
            _line("Published head", reason),
        ],
    )


def verify(
    rpc: RpcClient,
    store: SQLiteEvidenceStore,
    address: str,
    *,
    full_hashes: bool = False,
    expected_signer: str | None = None,
) -> AnchorOutcome:
    """Compare the local audit chain with the latest published anchor."""
    report = store.verify_audit_chain(expected_signer)
    try:
        chain_id = int(rpc.call("eth_chainId", []), 16)
    except Exception:
        return _unreadable(_network_label(ARC_TESTNET_CHAIN_ID), _local_summary(report), "unavailable (RPC unreachable)")
    network = _network_label(chain_id)

    if not report.get("ok"):
        return AnchorOutcome(
            "FAIL",
            1,
            [
                "FAIL  local audit chain does not verify, nothing is compared",
                _line("Network", network),
                _line("Local chain", f"broken at entry {report.get('first_broken_id')}"),
            ],
        )

    if chain_id != ARC_TESTNET_CHAIN_ID:
        return _unreadable(network, _local_summary(report), f"unavailable (RPC is {network}, not Arc Testnet)")

    try:
        event = read_latest_anchor(rpc, address)
    except Exception:
        return _unreadable(network, _local_summary(report), "unavailable (RPC unreachable)")
    if event is None:
        return _unreadable(network, _local_summary(report), "unavailable (no anchor published)")
    if event.chain_id != chain_id:
        return _unreadable(network, _local_summary(report), f"unavailable (anchor names chain {event.chain_id})")
    if event.application_id.lower() != ANCHOR_APPLICATION_ID.lower():
        return AnchorOutcome(
            "INCONCLUSIVE",
            3,
            [
                "INCONCLUSIVE  the published anchor is for something else, nothing is compared",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Published anchor", f"application id {short_hash(event.application_id)}"),
            ],
        )

    checked = int(report.get("checked") or 0)
    anchored = event.entries
    if anchored < 1 or anchored > checked:
        return AnchorOutcome(
            "FAIL",
            1,
            [
                "FAIL  the anchored entry count is not usable",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Anchored entries", str(anchored)),
            ],
        )
    local_head = head_at_entry(store, anchored)
    if local_head is None:
        return AnchorOutcome(
            "FAIL",
            1,
            [
                "FAIL  the local chain does not verify at the anchored entry",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Anchored entries", str(anchored)),
            ],
        )

    anchored_at = f"block {event.block_number}, anchor version {event.version}"
    if local_head.lower() == event.head.lower():
        newer = checked - anchored
        coverage = f"{anchored}; the {newer} newer entries are not covered" if newer > 0 else str(anchored)
        return AnchorOutcome(
            "MATCH",
            0,
            [
                f"MATCH  local audit head equals the published head at entry {anchored}",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Anchored at", anchored_at),
                _line("Anchored entries", coverage),
                _line(f"Local head @{anchored}", short_hash(local_head, full_hashes=full_hashes)),
                _line("Published head", short_hash(event.head, full_hashes=full_hashes)),
                _line("Not proven", "business truth, or anything before the first anchor"),
            ],
        )
    return AnchorOutcome(
        "MISMATCH",
        1,
        [
            f"MISMATCH  local audit head differs from the published head at entry {anchored}",
            _line("Network", network),
            _line("Local chain", _local_summary(report)),
            _line("Anchored at", anchored_at),
            _line("Anchored entries", str(anchored)),
            # Both heads whole: the reader has to compare them.
            f"Local head @{anchored}:",
            f"  {local_head}",
            "Published head:",
            f"  {event.head}",
        ],
    )


def _encode_publish(head: str, entries: int, version: int) -> str:
    body = encode(["bytes32", "uint64", "uint32"], [bytes.fromhex(head.removeprefix("0x")), entries, version])
    return PUBLISH_SELECTOR + body.hex()


def _wait_for_receipt(
    rpc: RpcClient,
    transaction_hash: str,
    *,
    timeout: float = 90.0,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            receipt = rpc.call("eth_getTransactionReceipt", [transaction_hash])
        except Exception:
            return None
        if isinstance(receipt, dict) and receipt.get("blockNumber"):
            return receipt
        sleep(poll)
    return None


def _wait_for_confirmation(
    rpc: RpcClient,
    block_number: int,
    *,
    timeout: float = 60.0,
    poll: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = int(rpc.call("eth_blockNumber", []), 16)
        except Exception:
            return False
        if current >= block_number + 1:
            return True
        sleep(poll)
    return False


def publish(
    store: SQLiteEvidenceStore,
    rpc: RpcClient,
    address: str,
    version: int,
    private_key: str,
    *,
    send: bool = False,
    full_hashes: bool = False,
    expected_signer: str | None = None,
) -> AnchorOutcome:
    """Verify the local chain, then publish its head. Without `send`, nothing leaves the machine."""
    report = store.verify_audit_chain(expected_signer)
    if not report.get("ok"):
        return AnchorOutcome(
            "REFUSED",
            2,
            [
                "REFUSED  local audit chain does not verify, nothing was published",
                _line(
                    "Local chain",
                    f"broken at entry {report.get('first_broken_id')} ({_reason_phrase(report.get('reason'))})",
                ),
            ],
        )
    if report.get("signature_anchor") != "configured_signer":
        return AnchorOutcome(
            "REFUSED",
            2,
            [
                "REFUSED  local audit chain signatures are not anchored, nothing was published",
                _line("Local chain", _local_summary(report)),
            ],
        )

    entries = int(report.get("checked") or 0)
    head = head_at_entry(store, entries)
    if head is None:
        return AnchorOutcome(
            "REFUSED",
            2,
            [
                "REFUSED  local audit chain has no verified head, nothing was published",
                _line("Local chain", _local_summary(report)),
            ],
        )

    try:
        chain_id = int(rpc.call("eth_chainId", []), 16)
    except Exception:
        return _unreadable(_network_label(ARC_TESTNET_CHAIN_ID), _local_summary(report), "unavailable (RPC unreachable)")
    network = _network_label(chain_id)
    if chain_id != ARC_TESTNET_CHAIN_ID:
        return _unreadable(network, _local_summary(report), f"unavailable (RPC is {network}, not Arc Testnet)")

    try:
        account = Account.from_key(private_key)
    except Exception:
        return AnchorOutcome("FAIL", 2, ["FAIL  AUDIT_ANCHOR_PRIVATE_KEY is not a usable key."])

    if not send:
        return AnchorOutcome(
            "DRY RUN",
            0,
            [
                f"DRY RUN  nothing sent; add --send to publish the head at entry {entries}",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Head to publish", short_hash(head, full_hashes=full_hashes)),
                _line("Signer", f"from environment, address {short_hash(account.address)}"),
                _line("Cost", "one transaction, paid in gas by the signer"),
            ],
        )

    try:
        transaction_hash = _send_publish(rpc, account, address, head, entries, version)
    except Exception:
        return AnchorOutcome(
            "INCONCLUSIVE",
            3,
            [
                "INCONCLUSIVE  the publish transaction was not confirmed, nothing is claimed",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Head to publish", short_hash(head, full_hashes=full_hashes)),
                _line("Published", "not sent (RPC unreachable)"),
            ],
        )

    receipt = _wait_for_receipt(rpc, transaction_hash)
    if receipt is None:
        return AnchorOutcome(
            "INCONCLUSIVE",
            3,
            [
                "INCONCLUSIVE  the publish transaction was not confirmed, nothing is claimed",
                _line("Network", network),
                _line("Local chain", _local_summary(report)),
                _line("Head to publish", short_hash(head, full_hashes=full_hashes)),
                _line("Published", f"tx {short_hash(transaction_hash)}, no receipt yet"),
            ],
        )
    block_number = _int_hex(receipt.get("blockNumber"))
    confirmed = _wait_for_confirmation(rpc, block_number)
    return AnchorOutcome(
        "PUBLISHED",
        0,
        [
            f"PUBLISHED  local audit head at entry {entries} is anchored on Arc Testnet",
            _line("Network", network),
            _line("Local chain", _local_summary(report)),
            _line("Head to publish", short_hash(head, full_hashes=full_hashes)),
            _line("Anchored at", f"block {block_number}, anchor version {version}"),
            f"Published:  tx {short_hash(transaction_hash, full_hashes=full_hashes)}, "
            + ("1 confirmation" if confirmed else "waiting for 1 confirmation"),
            _line("Not proven", "business truth, or anything before the first anchor"),
        ],
    )


def _send_publish(rpc: RpcClient, account: Any, address: str, head: str, entries: int, version: int) -> str:
    """Sign and send one publish transaction, returning its hash. Never returns the key."""
    data = _encode_publish(head, entries, version)
    nonce = int(rpc.call("eth_getTransactionCount", [account.address, "pending"]), 16)
    gas_price = int(rpc.call("eth_gasPrice", []), 16)
    try:
        estimated = int(rpc.call("eth_estimateGas", [{"from": account.address, "to": address, "value": 0, "data": data}]), 16)
    except Exception:
        estimated = 0
    transaction = {
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": max(estimated * 12 // 10, MINIMUM_GAS),
        "to": address,
        "value": 0,
        "data": data,
        "chainId": int(rpc.call("eth_chainId", []), 16),
    }
    signed = account.sign_transaction(transaction)
    return str(rpc.call("eth_sendRawTransaction", ["0x" + signed.raw_transaction.hex()]))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify the global audit-chain head against the published Arc anchor, or publish a new head"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    verify_parser = commands.add_parser("verify", help="Read-only: compare the local chain with the published anchor")
    verify_parser.add_argument("--full-hashes", action="store_true", help="Print hashes whole")
    publish_parser = commands.add_parser("publish", help="Verify the local chain, then publish its head")
    publish_parser.add_argument("--send", action="store_true", help="Sign and send one transaction; without it, a dry run")
    publish_parser.add_argument("--full-hashes", action="store_true", help="Print hashes whole")
    return parser


def main(argv: list[str] | None = None) -> None:
    from .settings import get_settings

    args = _build_parser().parse_args(argv)
    try:
        config = config_from_environment()
    except AnchorConfigurationError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    key = None
    if args.command == "publish":
        key = os.environ.get("AUDIT_ANCHOR_PRIVATE_KEY") or ""
        if not key:
            print("FAIL  AUDIT_ANCHOR_PRIVATE_KEY is not set.", file=sys.stderr)
            raise SystemExit(2)

    store = SQLiteEvidenceStore(get_settings().database_path)
    rpc = RpcClient(config.rpc_url)
    if args.command == "publish":
        outcome = publish(
            store,
            rpc,
            config.address,
            config.version,
            key,
            send=args.send,
            full_hashes=args.full_hashes,
            expected_signer=config.signer,
        )
    else:
        outcome = verify(rpc, store, config.address, full_hashes=args.full_hashes, expected_signer=config.signer)
    for line in outcome.lines:
        print(line)
    raise SystemExit(outcome.exit_code)


if __name__ == "__main__":
    main()
