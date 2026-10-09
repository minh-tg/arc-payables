"""The audit chain: append-only was not enough.

Events were already append-only by convention, but nothing stopped a rewrite of the table.
These tests treat the database as an adversary: they edit, delete and reorder rows directly
and require the verifier to notice.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arc_payables.mock_adapters import MockAccountingConnector
from arc_payables.policy import DeterministicPolicy
from arc_payables.currency import USDCOnlyConverter
from arc_payables.security import EIP712PermitSigner, recover_digest_signer
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import GENESIS_HASH, SQLiteEvidenceStore

POLICY_KEY = "0x" + "11" * 32


def _runtime(tmp_path: Path, *, signed: bool = True):
    settings = Settings(_env_file=None, database_path=tmp_path / "audit.sqlite3")
    signer = EIP712PermitSigner(POLICY_KEY)
    store = SQLiteEvidenceStore(settings.database_path, audit_signer=signer if signed else None)
    store.initialize()
    legitimate_id, suspicious_id = seed_demo(store)
    accounting = MockAccountingConnector(store)
    from arc_payables.mock_adapters import MockPaymentProvider

    payment = MockPaymentProvider(store)
    workflow = APWorkflow(store, accounting, payment, signer, DeterministicPolicy(settings, USDCOnlyConverter()), settings)
    workflow.evaluate(legitimate_id)
    workflow.submit_payment(legitimate_id)
    return store, legitimate_id, suspicious_id


def _chain_rows(store: SQLiteEvidenceStore) -> list[dict]:
    with store._connect() as connection:
        return [dict(row) for row in connection.execute("SELECT * FROM audit_events ORDER BY id").fetchall()]


def _evaluate_suspicious(store, suspicious_id: str) -> None:
    """Append more events through a real workflow decision."""
    from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
    from arc_payables.policy import DeterministicPolicy
    from arc_payables.currency import USDCOnlyConverter
    from arc_payables.service import APWorkflow
    from arc_payables.settings import Settings

    signer = EIP712PermitSigner(POLICY_KEY)
    settings = Settings(_env_file=None, database_path=store.path)
    APWorkflow(
        store,
        MockAccountingConnector(store),
        MockPaymentProvider(store),
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter()),
        settings,
    ).evaluate(suspicious_id)


def test_a_normal_flow_produces_a_verifiable_signed_chain(tmp_path):
    store, _, _ = _runtime(tmp_path)
    result = store.verify_audit_chain()
    assert result["ok"] is True
    assert result["length"] == result["checked"] > 0
    assert result["signed"] == result["checked"]
    assert result["unchained_prefix"] == 0
    assert result["signature_anchor"] == "configured_signer"
    assert result["head"] and result["head"] != GENESIS_HASH


def test_each_entry_links_to_the_previous_one(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    assert rows[0]["prev_hash"] == GENESIS_HASH
    for previous, current in zip(rows, rows[1:]):
        assert current["prev_hash"] == previous["event_hash"]


def test_the_chain_head_changes_as_events_are_appended(tmp_path):
    store, _, suspicious_id = _runtime(tmp_path)
    head = store.audit_head()
    length = len(_chain_rows(store))
    _evaluate_suspicious(store, suspicious_id)
    assert store.audit_head() != head
    assert len(_chain_rows(store)) > length


def test_an_edited_payload_is_detected(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    target = rows[len(rows) // 2]
    payload = json.loads(target["payload_json"])
    payload["tampered"] = True
    with store._connect() as connection:
        connection.execute("UPDATE audit_events SET payload_json=? WHERE id=?", (json.dumps(payload), target["id"]))
    result = store.verify_audit_chain()
    assert result["ok"] is False and result["first_broken_id"] == target["id"]
    assert result["reason"] == "entry_contents_do_not_match_its_hash"


def test_a_deleted_entry_is_detected(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    with store._connect() as connection:
        connection.execute("DELETE FROM audit_events WHERE id=?", (rows[1]["id"],))
    result = store.verify_audit_chain()
    assert result["ok"] is False
    assert result["reason"] == "prev_hash_does_not_match_the_previous_entry"


def test_a_reordered_entry_is_detected(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    # Swap the contents of two neighbouring entries, leaving their hashes in place.
    with store._connect() as connection:
        connection.execute(
            "UPDATE audit_events SET payload_json=?, event_type=? WHERE id=?",
            (rows[2]["payload_json"], rows[2]["event_type"], rows[1]["id"]),
        )
    result = store.verify_audit_chain()
    assert result["ok"] is False and result["first_broken_id"] == rows[1]["id"]


def test_a_rewritten_chain_without_the_key_is_detected(tmp_path):
    """An attacker who recomputes every hash consistently still cannot forge the signatures."""
    store, _, _ = _runtime(tmp_path)
    attacker = EIP712PermitSigner("0x" + "22" * 32)
    rows = _chain_rows(store)
    with store._connect() as connection:
        previous = GENESIS_HASH
        for row in rows:
            entry = {
                "invoice_id": row["invoice_id"],
                "event_type": row["event_type"],
                "state": row["state"],
                "payload_json": row["payload_json"],
                "created_at": row["created_at"],
                "prev_hash": previous,
            }
            from arc_payables.store import _event_digest

            digest = _event_digest(entry)
            event_hash = "0x" + digest.hex()
            # Perfectly consistent hashes, signed by a key that is not the authorized one.
            connection.execute(
                "UPDATE audit_events SET prev_hash=?, event_hash=?, signature=?, signer=? WHERE id=?",
                (previous, event_hash, attacker.sign_digest(digest), attacker.address, row["id"]),
            )
            previous = event_hash
    result = store.verify_audit_chain()
    # The chain is self-consistent, but the recorded signer is not the configured one.
    assert result["ok"] is False
    assert result["reason"] in {"signature_is_not_from_the_configured_signer", "recorded_signer_is_not_the_configured_signer"}


def test_an_unsigned_chain_verifies_but_reports_no_signatures(tmp_path):
    store, _, _ = _runtime(tmp_path, signed=False)
    result = store.verify_audit_chain()
    assert result["ok"] is True
    assert result["signed"] == 0


def test_a_legacy_unchained_prefix_is_surfaced(tmp_path):
    store, _, _ = _runtime(tmp_path, signed=False)
    rows = _chain_rows(store)
    with store._connect() as connection:
        connection.execute(
            "UPDATE audit_events SET prev_hash=NULL, event_hash=NULL, signature=NULL, signer=NULL WHERE id=?",
            (rows[0]["id"],),
        )
    result = store.verify_audit_chain()
    # The first entry is now unchained, which is reported rather than silently accepted.
    assert result["ok"] is False and result["first_broken_id"] == rows[1]["id"]


def test_recovered_signer_matches_the_configured_key(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    signer = EIP712PermitSigner(POLICY_KEY)
    assert recover_digest_signer(bytes.fromhex(rows[0]["event_hash"][2:]), rows[0]["signature"]).lower() == signer.address.lower()
    assert all(row["signer"].lower() == signer.address.lower() for row in rows)


def test_the_verify_endpoint_reports_the_chain(tmp_path):
    from fastapi.testclient import TestClient

    from arc_payables.api import create_app

    settings = Settings(_env_file=None, database_path=tmp_path / "endpoint.sqlite3", api_key="test-key")
    app = create_app(settings=settings)
    client = TestClient(app)
    assert client.get("/audit/verify").status_code == 401
    response = client.get("/audit/verify", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["signed"] == body["checked"]


def test_a_corrupted_signature_byte_is_detected(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    target = rows[1]
    sig = target["signature"]
    corrupted_char = "0" if sig[-1] != "0" else "1"
    corrupted_sig = sig[:-1] + corrupted_char
    with store._connect() as connection:
        connection.execute("UPDATE audit_events SET signature=? WHERE id=?", (corrupted_sig, target["id"]))
    result = store.verify_audit_chain()
    assert result["ok"] is False
    assert result["first_broken_id"] == target["id"]
    assert result["reason"] == "signature_is_not_from_the_configured_signer"


def test_a_tampered_metadata_field_is_detected(tmp_path):
    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    target = rows[0]
    with store._connect() as connection:
        connection.execute("UPDATE audit_events SET event_type='FORGED_EVENT' WHERE id=?", (target["id"],))
    result = store.verify_audit_chain()
    assert result["ok"] is False
    assert result["first_broken_id"] == target["id"]
    assert result["reason"] == "entry_contents_do_not_match_its_hash"


def test_the_verify_endpoint_reports_tampered_audit_chain_with_broken_id(tmp_path):
    from fastapi.testclient import TestClient

    from arc_payables.api import create_app

    store, _, _ = _runtime(tmp_path)
    rows = _chain_rows(store)
    target = rows[1]
    with store._connect() as connection:
        connection.execute("UPDATE audit_events SET event_type='FORGED_TYPE' WHERE id=?", (target["id"],))

    settings = Settings(_env_file=None, database_path=store.path, api_key="test-key")
    signer = EIP712PermitSigner(POLICY_KEY)
    app = create_app(settings=settings, store=store, signer=signer)
    client = TestClient(app)
    response = client.get("/audit/verify", headers={"X-API-Key": "test-key"})
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["first_broken_id"] == target["id"]
    assert body["reason"] == "entry_contents_do_not_match_its_hash"

