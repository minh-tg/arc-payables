"""Offline validator tamper checks use disposable copies, never historical evidence edits."""
from __future__ import annotations

import importlib.util
import json
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("evidence_check", ROOT / "script/check-evidence.py")
evidence_check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evidence_check)


@pytest.fixture
def snapshots(tmp_path, monkeypatch):
    for path in (ROOT / "docs/evidence").glob("*.json"):
        shutil.copyfile(path, tmp_path / path.name)
    monkeypatch.setattr(evidence_check, "EVIDENCE", tmp_path)
    return tmp_path


def test_saved_evidence_passes_without_claiming_missing_provenance(snapshots, capsys):
    evidence_check.main()
    assert "NOT VERIFIED: historical app/approval record" in capsys.readouterr().out


@pytest.mark.parametrize("file,field,value,error", [
    ("arc-testnet-settlement.json", ("requests", 0, "method"), "eth_sendRawTransaction", "Non-read-only"),
    ("arc-testnet-settlement.json", ("responses", 0, "result"), "0x1", "Wrong chain"),
    ("arc-testnet-settlement.json", ("decoded", "payment_executed", "amount_units"), 10001, "guard event mismatch"),
    ("circle-settlement.json", ("wallet", "address"), "0x1111111111111111111111111111111111111111", "payer provenance"),
    ("erpnext-sandbox-settlement.json", ("gl_entries", 0, "debit"), 0.03, "Unbalanced saved GL"),
    ("arc-testnet-allowance-history.json", ("inferred_pair", "exact_native_cost_units"), "1", "arithmetic mismatch"),
])
def test_validator_detects_corrupted_disposable_snapshots(snapshots, file, field, value, error):
    path = snapshots / file
    obj = json.loads(path.read_text())
    target = obj
    for component in field[:-1]:
        target = target[component]
    target[field[-1]] = value
    path.write_text(json.dumps(obj))
    with pytest.raises(ValueError, match=error):
        evidence_check.main()
