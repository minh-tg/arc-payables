"""The public audit-chain anchor: a rewrite the database cannot see still has to match it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest
from eth_abi import encode
from eth_utils import keccak

from arc_payables import anchor
from arc_payables.currency import USDCOnlyConverter
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import GENESIS_HASH, SQLiteEvidenceStore, _event_digest

ROOT = Path(__file__).resolve().parents[1]
POLICY_KEY = "0x" + "11" * 32
SIGNING_KEY = "0x" + "22" * 32
SIGNER_ADDRESS = "0x" + "33" * 20
ANCHOR_ADDRESS = "0x" + "44" * 20
PUBLISHER = "0x" + "55" * 20


def _store(tmp_path: Path, *, signed: bool = True) -> SQLiteEvidenceStore:
    settings = Settings(_env_file=None, database_path=tmp_path / "anchor.sqlite3")
    signer = EIP712PermitSigner(POLICY_KEY)
    store = SQLiteEvidenceStore(settings.database_path, audit_signer=signer if signed else None)
    store.initialize()
    legitimate_id, suspicious_id = seed_demo(store)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        MockPaymentProvider(store),
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter()),
        settings,
    )
    workflow.evaluate(legitimate_id)
    workflow.submit_payment(legitimate_id)
    workflow.evaluate(suspicious_id)
    return store


def _anchor_log(
    head: str,
    entries: int,
    *,
    version: int = 1,
    chain_id: int = 5042002,
    block: int = 65_000_000,
    index: int = 0,
    application_id: str = anchor.ANCHOR_APPLICATION_ID,
):
    return {
        "address": ANCHOR_ADDRESS,
        "topics": [anchor.ANCHORED_EVENT_TOPIC, application_id, head, "0x" + "00" * 12 + PUBLISHER[2:]],
        "data": "0x" + encode(["uint64", "uint32", "uint256"], [entries, version, chain_id]).hex(),
        "blockNumber": hex(block),
        "logIndex": hex(index),
        "transactionHash": "0x" + "cd" * 32,
    }


def _rpc(logs=(), *, chain_id: int = 5042002, unreachable: bool = False, calls: list[str] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        payload = request.content.decode()
        method = json.loads(payload)["method"]
        if calls is not None:
            calls.append(method)
        if unreachable:
            raise httpx.ConnectError("rpc unreachable", request=request)
        result = {
            "eth_chainId": hex(chain_id),
            "eth_getLogs": list(logs),
            "eth_getTransactionCount": hex(7),
            "eth_gasPrice": hex(10**9),
            "eth_estimateGas": hex(60_000),
            "eth_sendRawTransaction": "0x" + "ab" * 32,
            "eth_getTransactionReceipt": {"blockNumber": hex(65_000_001)},
            "eth_blockNumber": hex(65_000_002),
        }[method]
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    return anchor.RpcClient("https://rpc.invalid", httpx.Client(transport=httpx.MockTransport(handler)))


def _text(outcome) -> str:
    return "\n".join(outcome.lines)


# --------------------------------------------------------------------------------------
# verify: match, newer entries, and the rewrites the database alone accepts
# --------------------------------------------------------------------------------------


def test_verify_matches_the_published_head_and_reports_uncovered_newer_entries(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]
    assert checked >= 6
    anchored = checked - 2
    head = anchor.head_at_entry(store, anchored)
    assert head

    outcome = anchor.verify(_rpc([_anchor_log(head, anchored)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "MATCH" and outcome.exit_code == 0
    text = _text(outcome)
    assert f"MATCH  local audit head equals the published head at entry {anchored}" in text
    assert f"the 2 newer entries are not covered" in text
    assert f"Local head @{anchored}:" in text and "Not proven:" in text
    assert head[:10] + "..." + head[-6:] in text

    full = anchor.verify(_rpc([_anchor_log(head, anchored)]), store, ANCHOR_ADDRESS, full_hashes=True)
    assert head in _text(full) and "..." not in _text(full)


def _rewrite(store: SQLiteEvidenceStore, mutate) -> None:
    """Rewrite the chain so it still hash-verifies: the case the anchor exists to catch."""
    with store._connect() as connection:
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT id,invoice_id,event_type,state,payload_json,created_at,prev_hash,event_hash,signature,signer "
                "FROM audit_events ORDER BY id"
            ).fetchall()
        ]
        rows = mutate(rows)
        connection.execute("DELETE FROM audit_events")
        previous = GENESIS_HASH
        for row in rows:
            digest = _event_digest({**row, "prev_hash": previous})
            row["prev_hash"] = previous
            row["event_hash"] = "0x" + digest.hex()
            connection.execute(
                "INSERT INTO audit_events(id,invoice_id,event_type,state,payload_json,created_at,prev_hash,event_hash,signature,signer) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    row["id"],
                    row["invoice_id"],
                    row["event_type"],
                    row["state"],
                    row["payload_json"],
                    row["created_at"],
                    row["prev_hash"],
                    row["event_hash"],
                    row.get("signature"),
                    row.get("signer"),
                ),
            )
            previous = row["event_hash"]


def _drop_middle(rows):
    return rows[:2] + rows[3:]


def _edit_middle(rows):
    rows[2]["payload_json"] = rows[2]["payload_json"] + " "
    return rows


def _swap_two(rows):
    rows[2]["payload_json"], rows[3]["payload_json"] = rows[3]["payload_json"], rows[2]["payload_json"]
    return rows


@pytest.mark.parametrize("mutate", [_drop_middle, _edit_middle, _swap_two])
def test_verify_reports_a_consistent_rewrite_as_a_mismatch(tmp_path, mutate):
    """Deleting, editing or reordering a row and recomputing the hashes still has to mismatch."""
    store = _store(tmp_path, signed=False)
    checked = store.verify_audit_chain()["checked"]
    head = anchor.head_at_entry(store, checked)
    assert head

    _rewrite(store, mutate)
    report = store.verify_audit_chain()
    assert report["ok"], "the rewrite was meant to still verify as a chain"
    rewritten_entries = report["checked"]

    outcome = anchor.verify(_rpc([_anchor_log(head, rewritten_entries)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "MISMATCH" and outcome.exit_code == 1
    text = _text(outcome)
    assert "Local head @" in text and "Published head:" in text
    # Both heads are printed whole, because the reader has to compare them.
    assert head in text


def test_verify_fails_when_an_entry_was_edited_without_recomputing(tmp_path):
    store = _store(tmp_path)
    with store._connect() as connection:
        row = connection.execute("SELECT id, payload_json FROM audit_events ORDER BY id LIMIT 1 OFFSET 2").fetchone()
        connection.execute("UPDATE audit_events SET payload_json=? WHERE id=?", (row["payload_json"] + "x", row["id"]))

    outcome = anchor.verify(_rpc([_anchor_log("0x" + "ab" * 32, 5)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "FAIL" and outcome.exit_code == 1
    assert "broken at entry" in _text(outcome)


def test_verify_fails_when_the_anchor_covers_more_entries_than_the_store_holds(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]

    outcome = anchor.verify(_rpc([_anchor_log("0x" + "ab" * 32, checked + 5)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "FAIL" and outcome.exit_code == 1
    assert "Anchored entries:   " + str(checked + 5) in _text(outcome)


# --------------------------------------------------------------------------------------
# verify: an unreadable source is never a match
# --------------------------------------------------------------------------------------


def test_verify_is_inconclusive_when_the_rpc_is_unreachable(tmp_path):
    outcome = anchor.verify(_rpc(unreachable=True), _store(tmp_path), ANCHOR_ADDRESS)

    assert outcome.verdict == "INCONCLUSIVE" and outcome.exit_code == 3
    assert "unavailable (RPC unreachable)" in _text(outcome)


def test_verify_is_inconclusive_when_no_anchor_was_published(tmp_path):
    outcome = anchor.verify(_rpc([]), _store(tmp_path), ANCHOR_ADDRESS)

    assert outcome.verdict == "INCONCLUSIVE" and outcome.exit_code == 3
    assert "unavailable (no anchor published)" in _text(outcome)


def test_verify_is_inconclusive_when_the_rpc_is_not_arc_testnet(tmp_path):
    store = _store(tmp_path)
    head = anchor.head_at_entry(store, store.verify_audit_chain()["checked"])

    outcome = anchor.verify(_rpc([_anchor_log(head, 5)], chain_id=1), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "INCONCLUSIVE" and outcome.exit_code == 3
    assert "not Arc Testnet" in _text(outcome)


def test_verify_is_inconclusive_when_the_anchor_names_another_chain(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]
    head = anchor.head_at_entry(store, checked)

    outcome = anchor.verify(_rpc([_anchor_log(head, checked, chain_id=1)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "INCONCLUSIVE" and outcome.exit_code == 3
    assert "anchor names chain 1" in _text(outcome)


def test_verify_is_inconclusive_when_the_anchor_is_for_another_application(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]
    head = anchor.head_at_entry(store, checked)
    foreign = "0x" + keccak(text="other-app/audit-anchor/v1").hex()

    outcome = anchor.verify(_rpc([_anchor_log(head, checked, application_id=foreign)]), store, ANCHOR_ADDRESS)

    assert outcome.verdict == "INCONCLUSIVE" and outcome.exit_code == 3
    assert "the published anchor is for something else" in _text(outcome)
    assert "MATCH" not in _text(outcome)


# --------------------------------------------------------------------------------------
# publish: refuse loudly, dry run silently
# --------------------------------------------------------------------------------------


def _broken_store(tmp_path):
    store = _store(tmp_path)
    with store._connect() as connection:
        row = connection.execute("SELECT id, payload_json FROM audit_events ORDER BY id LIMIT 1").fetchone()
        connection.execute("UPDATE audit_events SET payload_json=? WHERE id=?", (row["payload_json"] + "x", row["id"]))
    return store


def test_publish_refuses_a_broken_chain_and_sends_nothing(tmp_path):
    calls: list[str] = []
    outcome = anchor.publish(
        _broken_store(tmp_path), _rpc([], calls=calls), ANCHOR_ADDRESS, 1, SIGNING_KEY, send=False
    )

    assert outcome.verdict == "REFUSED" and outcome.exit_code == 2
    assert "nothing was published" in _text(outcome)
    assert "eth_sendRawTransaction" not in calls


def test_publish_refuses_a_chain_without_an_anchored_signer(tmp_path):
    calls: list[str] = []
    outcome = anchor.publish(
        _store(tmp_path, signed=False), _rpc([], calls=calls), ANCHOR_ADDRESS, 1, SIGNING_KEY, send=False
    )

    assert outcome.verdict == "REFUSED" and outcome.exit_code == 2
    assert "signatures are not anchored" in _text(outcome)
    assert "eth_sendRawTransaction" not in calls


def test_publish_dry_run_sends_nothing(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]
    calls: list[str] = []

    outcome = anchor.publish(store, _rpc([], calls=calls), ANCHOR_ADDRESS, 1, SIGNING_KEY, send=False)

    assert outcome.verdict == "DRY RUN" and outcome.exit_code == 0
    text = _text(outcome)
    assert f"add --send to publish the head at entry {checked}" in text
    assert "Head to publish:" in text and "Cost:" in text
    assert "Signer:             from environment, address 0x" in text
    assert SIGNING_KEY not in text
    assert calls == ["eth_chainId"] or "eth_sendRawTransaction" not in calls


def test_publish_send_signs_one_transaction_for_the_head(tmp_path):
    store = _store(tmp_path)
    checked = store.verify_audit_chain()["checked"]
    head = anchor.head_at_entry(store, checked)
    calls: list[str] = []
    raw_transactions: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode())
        calls.append(payload["method"])
        if payload["method"] == "eth_chainId":
            result = hex(5042002)
        elif payload["method"] == "eth_getTransactionCount":
            result = hex(7)
        elif payload["method"] == "eth_gasPrice":
            result = hex(10**9)
        elif payload["method"] == "eth_estimateGas":
            result = hex(60_000)
        elif payload["method"] == "eth_sendRawTransaction":
            raw_transactions.append(payload["params"][0])
            result = "0x" + "ab" * 32
        elif payload["method"] == "eth_getTransactionReceipt":
            result = {"blockNumber": hex(65_000_001)}
        elif payload["method"] == "eth_blockNumber":
            result = hex(65_000_002)
        else:
            raise AssertionError(payload["method"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})

    rpc = anchor.RpcClient("https://rpc.invalid", httpx.Client(transport=httpx.MockTransport(handler)))
    outcome = anchor.publish(store, rpc, ANCHOR_ADDRESS, 1, SIGNING_KEY, send=True)

    assert outcome.verdict == "PUBLISHED" and outcome.exit_code == 0
    text = _text(outcome)
    assert "Published:  tx 0x" in text and "1 confirmation" in text
    assert SIGNING_KEY not in text
    assert calls.count("eth_sendRawTransaction") == 1
    # The one transaction calls publish on the anchor contract with the head.
    assert len(raw_transactions) == 1 and raw_transactions[0].startswith("0x")
    assert anchor.PUBLISH_SELECTOR[2:].lower() in raw_transactions[0].lower()
    assert ANCHOR_ADDRESS[2:].lower() in raw_transactions[0].lower()
    assert head[2:].lower() in raw_transactions[0].lower()


def test_the_signing_key_never_appears_in_an_error_message(tmp_path):
    store = _store(tmp_path)
    outcome = anchor.publish(store, _rpc([], unreachable=True), ANCHOR_ADDRESS, 1, SIGNING_KEY, send=True)
    assert outcome.verdict == "INCONCLUSIVE"
    assert SIGNING_KEY not in _text(outcome)

    broken_key = "0x" + "zz" * 32
    outcome = anchor.publish(store, _rpc([]), ANCHOR_ADDRESS, 1, broken_key, send=False)
    assert outcome.verdict == "FAIL" and outcome.exit_code == 2
    assert broken_key not in _text(outcome)


# --------------------------------------------------------------------------------------
# configuration and the command line
# --------------------------------------------------------------------------------------


def test_configuration_names_missing_and_invalid_values():
    with pytest.raises(anchor.AnchorConfigurationError):
        anchor.config_from_environment({})
    with pytest.raises(anchor.AnchorConfigurationError):
        anchor.config_from_environment({"AUDIT_ANCHOR_ADDRESS": "not-an-address"})
    with pytest.raises(anchor.AnchorConfigurationError):
        anchor.config_from_environment({"AUDIT_ANCHOR_ADDRESS": ANCHOR_ADDRESS, "AUDIT_ANCHOR_VERSION": "x"})
    config = anchor.config_from_environment({"AUDIT_ANCHOR_ADDRESS": ANCHOR_ADDRESS})
    assert config.rpc_url == anchor.DEFAULT_ANCHOR_RPC_URL and config.version == 1
    assert config.signer is None
    assert anchor.config_from_environment(
        {"AUDIT_ANCHOR_ADDRESS": ANCHOR_ADDRESS, "AUDIT_ANCHOR_SIGNER": SIGNER_ADDRESS}
    ).signer == SIGNER_ADDRESS


def test_the_command_line_never_echoes_the_signing_key(tmp_path):
    store = _store(tmp_path)
    environment = {
        **os.environ,
        "DATABASE_PATH": str(store.path),
        "AUDIT_ANCHOR_ADDRESS": ANCHOR_ADDRESS,
        "AUDIT_ANCHOR_SIGNER": EIP712PermitSigner(POLICY_KEY).address,
        "AUDIT_ANCHOR_RPC_URL": "http://127.0.0.1:9",
        "AUDIT_ANCHOR_PRIVATE_KEY": SIGNING_KEY,
        "PYTHONPATH": str(ROOT / "src"),
    }
    result = subprocess.run(
        [sys.executable, "-m", "arc_payables.anchor", "publish", "--send"],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
    )

    assert result.returncode == 3
    assert "INCONCLUSIVE" in result.stdout
    assert SIGNING_KEY not in result.stdout and SIGNING_KEY not in result.stderr
