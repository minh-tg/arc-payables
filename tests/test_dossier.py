"""The payment dossier: export what the permit signed, verify it offline, refuse everything else.

The verifier treats the file as the only input. These tests rewrite the file the way an editor
would and require every rewritten field to fail, and they check that a refusal writes nothing.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from arc_payables import dossier
from arc_payables.currency import USDCOnlyConverter
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

POLICY_KEY = "0x" + "11" * 32


def _runtime(tmp_path: Path, *, failure_mode: str | None = None):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "dossier.sqlite3",
        payment_provider="mock",
        accounting_provider="mock",
    )
    signer = EIP712PermitSigner(POLICY_KEY)
    store = SQLiteEvidenceStore(settings.database_path, audit_signer=signer)
    store.initialize()
    invoice_id, _ = seed_demo(store)
    provider = MockPaymentProvider(store, signer=signer, failure_mode=failure_mode)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        provider,
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter()),
        settings,
    )
    workflow.evaluate(invoice_id)
    return settings, store, workflow, invoice_id


def _settled(tmp_path: Path, *, failure_mode: str | None = None):
    settings, store, workflow, invoice_id = _runtime(tmp_path, failure_mode=failure_mode)
    workflow.submit_payment(invoice_id)
    return settings, store, invoice_id


def _exported(tmp_path: Path) -> tuple[list[str], dict]:
    settings, store, invoice_id = _settled(tmp_path)
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 0
    return lines, json.loads((tmp_path / "dossier.json").read_text())


def _rewrite(tmp_path: Path, document: dict) -> dossier.VerifyOutcome:
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(document, default=dossier._json_default))
    return dossier.verify(path)


def test_export_then_verify_round_trip(tmp_path):
    settings, store, invoice_id = _settled(tmp_path)
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 0
    assert lines[0].startswith("EXPORTED  dossier for ACME-INV-2026-001,")

    outcome = dossier.verify(tmp_path / "dossier.json")
    assert outcome.verdict == "VERIFIED"
    assert outcome.exit_code == 0
    assert outcome.lines[0] == "VERIFIED  3 of 3 offline checks passed, 4 facts not checked"
    assert sum(1 for line in outcome.lines if "Not checked:" in line or line.startswith("                    ")) >= 4


def test_export_line_1_names_the_invoice_and_the_count(tmp_path):
    lines, document = _exported(tmp_path)
    expected = f"EXPORTED  dossier for ACME-INV-2026-001, {len(document['audit']['entries'])} audit entries"
    assert lines[0] == f"{expected}, not yet verified"


def test_export_labels_the_payment_and_the_signer(tmp_path):
    lines, document = _exported(tmp_path)
    joined = "\n".join(lines)
    assert "Payment:            settled, 250.00 USDC, simulated (no on-chain transaction)" in joined
    assert "Signer in file:     0x" in joined
    assert "(claimed by the file, not proven)" in joined
    assert "not yet verified" in lines[0]


def test_verify_reports_what_it_did_not_check(tmp_path):
    lines, document = _exported(tmp_path)
    outcome = dossier.verify(tmp_path / "dossier.json")
    assert any("that the named signer may sign permits" in line for line in outcome.lines)
    assert any("that the payment is real: it is simulated, no transaction exists" in line for line in outcome.lines)
    assert any("that the ERP documents exist or match their references" in line for line in outcome.lines)
    assert any("that the file holds every audit entry for this invoice" in line for line in outcome.lines)
    assert outcome.lines[-1].startswith("Source:             this file only; no network")


def test_a_dossier_carries_the_signed_permit_and_the_evidence(tmp_path):
    _lines, document = _exported(tmp_path)
    assert document["schema"] == 1
    assert document["kind"] == "arc-payables-payment-dossier"
    assert document["payment"]["confirmation_status"] == "CONFIRMED"
    assert document["payment"]["signature"].startswith("0x")
    assert document["payment"]["permit"]["evidence_hash"] == document["evidence"]["hash"]
    invoice_ids = {entry["invoice_id"] for entry in document["audit"]["entries"]}
    assert document["invoice"]["id"] in invoice_ids
    assert document["audit"]["entries"][0]["prev_hash"] == "0x" + "00" * 32


def test_export_refuses_an_invoice_with_no_payment(tmp_path):
    settings, store, _workflow, invoice_id = _runtime(tmp_path)
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 2
    assert lines[0] == "REFUSED  no settled payment is recorded for this invoice, nothing written"
    assert not (tmp_path / "dossier.json").exists()


def test_export_refuses_an_unsettled_payment(tmp_path):
    settings, store, workflow, invoice_id = _runtime(tmp_path, failure_mode="uncertain")
    workflow.submit_payment(invoice_id)
    payment = store.get_payment(invoice_id)
    assert payment is not None and payment["confirmation_status"] != "CONFIRMED"
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 2
    assert "export needs a recorded settlement" in "\n".join(lines)
    assert not (tmp_path / "dossier.json").exists()


def test_export_refuses_a_missing_evidence_snapshot(tmp_path):
    settings, store, invoice_id = _settled(tmp_path)
    with store._connect() as connection:
        connection.execute("UPDATE invoices SET evidence_snapshot_json=NULL WHERE id=?", (invoice_id,))
        connection.commit()
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 2
    assert lines[0] == "REFUSED  the evidence snapshot is missing, nothing written"


def test_export_refuses_a_missing_permit(tmp_path):
    settings, store, invoice_id = _settled(tmp_path)
    with store._connect() as connection:
        row = connection.execute("SELECT record_json FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()
        payment = json.loads(row["record_json"])
        payment.pop("permit")
        connection.execute("UPDATE payments SET record_json=? WHERE invoice_id=?", (json.dumps(payment), invoice_id))
        connection.commit()
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 2
    assert lines[0] == "REFUSED  the signed permit is missing, nothing written"


def test_export_refuses_missing_audit_entries(tmp_path):
    settings, store, invoice_id = _settled(tmp_path)
    with store._connect() as connection:
        connection.execute("DELETE FROM audit_events WHERE invoice_id=?", (invoice_id,))
        connection.commit()
    lines, code = dossier.export(store, settings, invoice_id, out=tmp_path / "dossier.json")
    assert code == 2
    assert lines[0] == "REFUSED  no audit entries are recorded for this invoice, nothing written"


def test_verify_rejects_an_unknown_schema(tmp_path):
    _lines, document = _exported(tmp_path)
    document["schema"] = 2
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "REFUSED"
    assert outcome.exit_code == 2
    assert outcome.lines[0] == "REFUSED  schema version 2 is not supported, nothing was checked"


def test_verify_rejects_a_missing_required_field(tmp_path):
    _lines, document = _exported(tmp_path)
    document.pop("evidence")
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "REFUSED"
    assert outcome.lines[0] == "REFUSED  field evidence is missing, nothing was checked"


def test_verify_rejects_a_missing_permit_hash(tmp_path):
    _lines, document = _exported(tmp_path)
    document["payment"]["permit"].pop("evidence_hash")
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "REFUSED"
    assert outcome.lines[0] == "REFUSED  field permit_hash is missing, nothing was checked"


def test_verify_rejects_a_malformed_hash(tmp_path):
    _lines, document = _exported(tmp_path)
    document["evidence"]["hash"] = "0xnothex"
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "REFUSED"
    assert outcome.lines[0] == "REFUSED  hash in field evidence_hash is malformed, nothing was checked"


def test_verify_rejects_a_file_that_is_not_json(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    outcome = dossier.verify(path)
    assert outcome.verdict == "REFUSED"
    assert outcome.exit_code == 2
    assert outcome.lines[0] == "REFUSED  file is not valid JSON, nothing was checked"


def test_verify_rejects_duplicate_entries(tmp_path):
    _lines, document = _exported(tmp_path)
    document["audit"]["entries"].insert(4, dict(document["audit"]["entries"][3]))
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "FAIL"
    assert outcome.exit_code == 1
    assert "entry ids are not strictly increasing at entry 5" in "\n".join(outcome.lines)


def test_verify_rejects_reordered_entries(tmp_path):
    _lines, document = _exported(tmp_path)
    entries = document["audit"]["entries"]
    entries[2], entries[3] = entries[3], entries[2]
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "FAIL"
    assert "does not link to entry 2" in "\n".join(outcome.lines)


TAMPERS = {
    "invoice_amount": lambda d: d["evidence"]["invoice"].__setitem__("amount_units", 1),
    "invoice_number": lambda d: d["evidence"]["invoice"].__setitem__("invoice_number", "ACME-INV-9999"),
    "supplier_wallet": lambda d: d["evidence"]["accounting"]["supplier"].__setitem__(
        "approved_wallet", "0x" + "22" * 20
    ),
    "treasury_balance": lambda d: d["evidence"]["treasury"].__setitem__("balance_units", 1),
    "screening_status": lambda d: d["evidence"]["screening"].__setitem__("status", "FLAGGED"),
    "evidence_hash": lambda d: d["evidence"].__setitem__("hash", "0x" + "ab" * 32),
    "evidence_and_hash": lambda d: (
        d["evidence"]["invoice"].__setitem__("amount_units", 1),
        d["evidence"].__setitem__("hash", "0x" + "ab" * 32),
    ),
    "permit_hash": lambda d: d["payment"]["permit"].__setitem__("evidence_hash", "0x" + "ab" * 32),
    "permit_amount": lambda d: d["payment"]["permit"].__setitem__("amount_units", 1),
    "permit_recipient": lambda d: d["payment"]["permit"].__setitem__("recipient", "0x" + "22" * 20),
    "permit_expiry": lambda d: d["payment"]["permit"].__setitem__("expiry", 1),
    "signature": lambda d: d["payment"].__setitem__("signature", "0x" + "ab" * 65),
    "signer": lambda d: d["payment"].__setitem__("signer", "0x" + "33" * 20),
    "audit_payload": lambda d: d["audit"]["entries"][3].__setitem__("payload", {"rewritten": True}),
    "audit_event_hash": lambda d: d["audit"]["entries"][3].__setitem__("event_hash", "0x" + "cd" * 32),
    "audit_prev_hash": lambda d: d["audit"]["entries"][3].__setitem__("prev_hash", "0x" + "cd" * 32),
    "audit_state": lambda d: d["audit"]["entries"][3].__setitem__("state", "HELD"),
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_verify_fails_on_a_tampered_verified_field(tmp_path, name):
    _lines, document = _exported(tmp_path)
    TAMPERS[name](document)
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "FAIL"
    assert outcome.exit_code == 1
    assert "offline checks passed" in outcome.lines[0]


def test_a_repaired_evidence_block_still_fails_the_permit_binding(tmp_path):
    _lines, document = _exported(tmp_path)
    # Change the evidence and make its own hash consistent again. The permit signature covers
    # the original hash, so this must still fail.
    from arc_payables.policy import compute_evidence_hash

    document["evidence"]["invoice"]["amount_units"] = 1
    invoice, accounting, treasury, screening = dossier._evidence_objects(document["evidence"])
    document["evidence"]["hash"] = compute_evidence_hash(invoice, accounting, treasury, screening)
    outcome = _rewrite(tmp_path, document)
    assert outcome.verdict == "FAIL"
    assert "the permit signs" in "\n".join(outcome.lines)


def test_verify_prints_full_hashes_with_the_flag(tmp_path):
    _lines, document = _exported(tmp_path)
    short = dossier.verify(tmp_path / "dossier.json")
    full = dossier.verify(tmp_path / "dossier.json", full_hashes=True)
    assert "..." in "\n".join(short.lines)
    assert document["evidence"]["hash"] in "\n".join(full.lines)


def test_verify_uses_no_network(tmp_path, monkeypatch):
    _lines, document = _exported(tmp_path)

    def refuse(*_args, **_kwargs):
        raise AssertionError("the verifier opened a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    outcome = dossier.verify(tmp_path / "dossier.json")
    assert outcome.verdict == "VERIFIED"


def test_main_verifies_a_file_and_returns_the_exit_code(tmp_path, capsys):
    _lines, document = _exported(tmp_path)
    code = dossier.main(["verify", str(tmp_path / "dossier.json")])
    printed = capsys.readouterr().out
    assert code == 0
    assert printed.startswith("VERIFIED")
