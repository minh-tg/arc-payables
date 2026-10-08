"""Managed signing: key rotation, compromise recovery and historical verification.

A policy key rotation is not a code change; it is an evidence-continuity question.
These tests require that rotating the signer keeps the historical audit chain
verifiable when the retired address is explicitly named, and that an *unlisted*
signer still fails exactly as an attacker's would. The guard's policy signer is
immutable, so a rotation also implies a new guard; that is documented, not simulated
here.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.currency import USDCOnlyConverter
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

OLD_KEY = "0x" + "11" * 32
NEW_KEY = "0x" + "22" * 32
ATTACKER_KEY = "0x" + "33" * 32


def _workflow(tmp_path: Path, signer):
    settings = Settings(_env_file=None, database_path=tmp_path / "rotation.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    store.set_audit_signer(signer)
    invoice_id, _ = seed_demo(store)
    payment = MockPaymentProvider(store, signer=signer)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        payment,
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    return settings, store, workflow, invoice_id


def test_rotation_keeps_history_verifiable_only_when_the_old_key_is_named(tmp_path):
    """The discriminating case: same database, new signer, explicit retired address."""
    old = EIP712PermitSigner(OLD_KEY)
    settings, store, workflow, invoice_id = _workflow(tmp_path, old)
    workflow.evaluate(invoice_id)
    assert store.verify_audit_chain()["ok"] is True
    assert store.verify_audit_chain()["signature_anchor"] == "configured_signer"
    signed_before = store.verify_audit_chain()["signed"]
    assert signed_before > 0

    # Rotate: reopen the same database with a new signer, as a restart after a key change.
    rotated = SQLiteEvidenceStore(settings.database_path)
    rotated.initialize()
    new = EIP712PermitSigner(NEW_KEY)
    rotated.set_audit_signer(new)
    current = rotated.verify_audit_chain()
    assert current["ok"] is False
    assert current["reason"] == "signature_is_not_from_the_configured_signer"

    # Naming the retired address is what makes the historical evidence verifiable again.
    rotated.set_audit_signer(new, retired_addresses=(old.address,))
    restored = rotated.verify_audit_chain()
    assert restored["ok"] is True
    assert restored["signed"] == signed_before
    assert restored["signature_anchor"] == "configured_signer"

    # The setting carries through the normal start-up path, not just the direct call.
    settings_runtime = Settings(
        _env_file=None,
        database_path=settings.database_path,
        permit_signing_private_key=NEW_KEY,
        permit_signing_retired_addresses=(old.address,),
    )
    from arc_payables.runtime import build_workflow

    rebuilt = build_workflow(
        settings_runtime,
        store=rotated,
        accounting=MockAccountingConnector(rotated),
        payment_provider=MockPaymentProvider(rotated, signer=new),
        signer=new,
    )
    assert rebuilt.store.verify_audit_chain()["ok"] is True


def test_retired_addresses_do_not_admit_an_unlisted_key(tmp_path):
    """Widening verification must not weaken it: an unlisted signer fails as before."""
    old = EIP712PermitSigner(OLD_KEY)
    _, store, workflow, invoice_id = _workflow(tmp_path, old)
    workflow.evaluate(invoice_id)

    # A legitimate rotation: new signer in use, old address retired, so both are accepted.
    rotated = SQLiteEvidenceStore(store.path)
    rotated.initialize()
    attacker = EIP712PermitSigner(ATTACKER_KEY)
    rotated.set_audit_signer(EIP712PermitSigner(NEW_KEY), retired_addresses=(old.address,))
    assert rotated.verify_audit_chain()["ok"] is True

    # The attacker holds a key that is neither in use nor retired.
    with sqlite3.connect(store.path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT id,event_hash FROM audit_events ORDER BY id").fetchall()
        assert rows, "the fixture must have produced signed entries"
        for row in rows:
            signature = attacker.sign_digest(bytes.fromhex(row["event_hash"][2:]))
            connection.execute("UPDATE audit_events SET signature=?, signer=? WHERE id=?",
                               (signature, attacker.address, row["id"]))
        connection.commit()
    forged = rotated.verify_audit_chain()
    assert forged["ok"] is False
    assert forged["reason"] == "signature_is_not_from_the_configured_signer"




def test_production_refuses_an_environment_held_policy_key():
    """Production must not keep the policy key next to the application, and must pin the address."""
    from pydantic import ValidationError

    from arc_payables.settings import Settings

    oidc = dict(
        oidc_issuer="https://identity.example.invalid",
        oidc_client_id="tameion",
        oidc_redirect_uri="https://payables.example.invalid/auth/callback",
        oidc_subject_roles={"checker": ("approver",)},
        oidc_mfa_values=("mfa",),
    )
    with pytest.raises(ValidationError, match="Production requires SIGNER_BACKEND=pkcs11"):
        Settings(_env_file=None, environment="production", auth_mode="oidc",
                 signer_backend="env", permit_signing_private_key=OLD_KEY,
                 permit_signing_address="0x" + "55" * 20, **oidc)
    with pytest.raises(ValidationError, match="Production requires PERMIT_SIGNING_ADDRESS"):
        Settings(_env_file=None, environment="production", auth_mode="oidc", signer_backend="pkcs11",
                 pkcs11_lib_path="/usr/lib/libsofthsm2.so", pkcs11_key_label="policy-sign",
                 pkcs11_user_pin="1234", **oidc)
    # Testnet and local development keep the environment backend, which the demo relies on.
    local = Settings(_env_file=None, permit_signing_private_key=OLD_KEY)
    assert local.signer_backend == "env"


def test_signed_and_unsigned_chains_are_distinguished_after_rotation(tmp_path):
    """A chain with no configured key at all reports unanchored rather than verified."""
    signer = EIP712PermitSigner(OLD_KEY)
    _, store, workflow, invoice_id = _workflow(tmp_path, signer)
    workflow.evaluate(invoice_id)
    unanchored = SQLiteEvidenceStore(store.path)
    unanchored.initialize()
    result = unanchored.verify_audit_chain()
    assert result["ok"] is True
    assert result["signature_anchor"] == "not_configured"
    assert result["unanchored_signatures"] == result["signed"] > 0
