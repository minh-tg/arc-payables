"""Managed PKCS#11 policy signing against a SoftHSM2 test double.

These tests need real PKCS#11 tools (softhsm2-util, pkcs11-tool) plus a SoftHSM2
library, so they skip unless TAMEION_TEST_PKCS11=1 with TAMEION_SOFTHSM_CONF,
TAMEION_PKCS11_LIB and TAMEION_PKCS11_TOOL set. Everything else stays hermetic.
No funds move: signatures are verified against the local EVM guard bytecode and the
audit chain, never broadcast.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from arc_payables.domain import PaymentPermit
from arc_payables.pkcs11_signer import (
    PKCS11Error,
    PKCS11KeyLocator,
    PKCS11PermitSigner,
    build_pkcs11_signer,
)
from arc_payables.security import EIP712PermitSigner, SignerBackendUnavailable, build_permit_signer
from arc_payables.settings import Settings

def _missing_prerequisite() -> str | None:
    """What is absent, or None when these tests can actually run."""
    if os.environ.get("TAMEION_TEST_PKCS11") != "1":
        return "TAMEION_TEST_PKCS11 is not 1"
    for name in ("TAMEION_SOFTHSM_CONF", "TAMEION_PKCS11_LIB", "TAMEION_PKCS11_TOOL", "TAMEION_SOFTHSM_UTIL"):
        if not os.environ.get(name):
            return f"{name} is not set"
    if not shutil.which(os.environ["TAMEION_SOFTHSM_UTIL"]):
        return f"{os.environ['TAMEION_SOFTHSM_UTIL']} is not executable"
    if not os.path.exists(os.environ["TAMEION_SOFTHSM_CONF"]):
        return f"{os.environ['TAMEION_SOFTHSM_CONF']} does not exist"
    if not os.path.exists(os.environ["TAMEION_PKCS11_LIB"]):
        return f"{os.environ['TAMEION_PKCS11_LIB']} does not exist"
    return None


_MISSING = _missing_prerequisite()

# A skipped token suite must never look like a passing one. Where these tests are the point
# of the job (TAMEION_REQUIRE_PKCS11=1), a missing prerequisite is a hard collection error
# rather than a silent skip: CI once reported success with every token test skipped because
# the library path pointed at a package that was not installed.
if _MISSING and os.environ.get("TAMEION_REQUIRE_PKCS11") == "1":
    raise RuntimeError(
        f"the PKCS#11 token tests were required but cannot run: {_MISSING}. "
        "Install the PKCS#11 provider and point TAMEION_PKCS11_LIB at it."
    )

pytestmark = pytest.mark.skipif(
    bool(_MISSING),
    reason=f"PKCS#11 SoftHSM2 integration tests are opt-in: {_MISSING}",
)

CONF = os.environ.get("TAMEION_SOFTHSM_CONF", "")
LIB = os.environ.get("TAMEION_PKCS11_LIB", "")
TOOL = os.environ.get("TAMEION_PKCS11_TOOL", "")
USER_PIN = "2345"
SO_PIN = "123456"


def _run(*args: str) -> str:
    env = dict(os.environ, SOFTHSM2_CONF=CONF)
    completed = subprocess.run([TOOL, *args], capture_output=True, text=True, env=env, timeout=60)
    assert completed.returncode == 0, completed.stderr.strip()
    return completed.stdout


@pytest.fixture(scope="module")
def token():
    util = os.environ.get("TAMEION_SOFTHSM_UTIL", "softhsm2-util")
    env = dict(os.environ, SOFTHSM2_CONF=CONF)
    # The harness may have initialized the token already; initializing twice must not fail the run.
    init = subprocess.run([util, "--init-token", "--slot", "0", "--label", "tameion-test",
                           "--so-pin", SO_PIN, "--pin", USER_PIN],
                          capture_output=True, text=True, env=env, timeout=60)
    shown = subprocess.run([util, "--show-slots"], capture_output=True, text=True, env=env, timeout=60)
    assert shown.returncode == 0, shown.stderr.strip()
    active = [line for line in shown.stdout.splitlines() if line.startswith("Slot ")][0].split()[1]
    assert "tameion-test" in shown.stdout, f"no test token available (init said: {init.stderr.strip()})"
    _run("--module", LIB, "--slot", active, "--login", "--pin", USER_PIN,
         "--keypairgen", "--key-type", "EC:secp256k1", "--id", "01",
         "--label", "test-policy", "--usage-sign", "--sensitive")
    listing = _run("--module", LIB, "--slot", active, "--login", "--pin", USER_PIN, "-O")
    assert "never extractable" in listing
    return {"slot": int(active)}


def _settings(**overrides):
    values = {
        "_env_file": None,
        "signer_backend": "pkcs11",
        "pkcs11_lib_path": LIB,
        "pkcs11_key_label": "test-policy",
        "pkcs11_key_id_hex": "01",
        "pkcs11_user_pin": USER_PIN,
        "pkcs11_slot": None,
    }
    values.update(overrides)
    settings = Settings(**values)
    settings.pkcs11_slot = values.get("pkcs11_slot")
    return settings


def _permit(**changes) -> PaymentPermit:
    values = {
        "payer": "0x1111111111111111111111111111111111111111",
        "token": "0x3600000000000000000000000000000000000000",
        "recipient": "0x2222222222222222222222222222222222222222",
        "amount_units": 250_000_000,
        "evidence_hash": "0x" + "ab" * 32,
        "payment_id": "0x" + "cd" * 32,
        "expiry": int(time.time()) + 300,
        "chain_id": 5_042_002,
        "guard_address": "0x3333333333333333333333333333333333333333",
    }
    values.update(changes)
    return PaymentPermit(**values)


def test_token_signatures_verify_and_stay_low_s(token):
    signer = PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="test-policy", key_id=b"\x01", slot=token["slot"]), USER_PIN)
    assert signer.backend == "pkcs11"
    assert signer.address.startswith("0x")
    permit = _permit()
    low = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 // 2
    seen = set()
    for _ in range(5):
        signature = signer.sign(permit)
        assert len(signature) == 132
        assert int(signature[66:130], 16) <= low
        assert signature[-2:] in ("1b", "1c")
        assert signer.verify(permit, signature)
        seen.add(signature)
    # Randomized nonces: repeated signing must not repeat the byte-identical signature.
    assert len(seen) > 1
    assert signer.sign_digest(b"\x11" * 32).startswith("0x")


def test_factory_builds_from_settings_and_pins_the_address(token):
    settings = _settings(pkcs11_slot=token["slot"])
    signer = build_permit_signer(settings)
    assert isinstance(signer, PKCS11PermitSigner)
    assert signer.address.startswith("0x")
    pinned = Settings(_env_file=None, signer_backend="env",
                      permit_signing_private_key="0x" + "42" * 32,
                      permit_signing_address="0x" + "00" * 20)
    with pytest.raises(SignerBackendUnavailable, match="does not match PERMIT_SIGNING_ADDRESS"):
        build_permit_signer(pinned)
    wrong = _settings(pkcs11_slot=token["slot"], permit_signing_address="0x" + "00" * 20)
    with pytest.raises(SignerBackendUnavailable, match="does not match PERMIT_SIGNING_ADDRESS"):
        build_pkcs11_signer(wrong)


def test_factory_fails_closed_without_configuration():
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_LIB_PATH"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11"))
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_KEY_LABEL"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11", pkcs11_lib_path=LIB))
    with pytest.raises(SignerBackendUnavailable, match="user PIN"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11",
                                     pkcs11_lib_path=LIB, pkcs11_key_label="test-policy"))
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_KEY_ID_HEX"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11",
                                     pkcs11_lib_path=LIB, pkcs11_key_label="test-policy",
                                     pkcs11_user_pin=USER_PIN, pkcs11_key_id_hex="zz"))
    with pytest.raises(SignerBackendUnavailable, match="development-only"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11",
                                     pkcs11_lib_path=LIB, pkcs11_key_label="test-policy",
                                     pkcs11_user_pin=USER_PIN, pkcs11_test_backend=True))


def test_wrong_pin_and_unknown_key_fail_closed(token):
    with pytest.raises(PKCS11Error):
        PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="test-policy", key_id=b"\x01", slot=token["slot"]), "wrong-pin")
    with pytest.raises(PKCS11Error, match="missing or ambiguous"):
        PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="no-such-key", key_id=b"\x01", slot=token["slot"]), USER_PIN)


def test_extractable_key_is_refused(token):
    slot = str(token["slot"])
    _run("--module", LIB, "--slot", slot, "--login", "--pin", USER_PIN,
         "--keypairgen", "--key-type", "EC:secp256k1", "--id", "02",
         "--label", "extractable-policy", "--usage-sign", "--sensitive", "--extractable")
    with pytest.raises(PKCS11Error, match="extractable"):
        PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="extractable-policy", key_id=b"\x02", slot=token["slot"]), USER_PIN)


def test_non_secp256k1_key_is_refused(token):
    slot = str(token["slot"])
    _run("--module", LIB, "--slot", slot, "--login", "--pin", USER_PIN,
         "--keypairgen", "--key-type", "EC:prime256v1", "--id", "03",
         "--label", "p256-policy", "--usage-sign", "--sensitive")
    with pytest.raises(PKCS11Error, match="not secp256k1"):
        PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="p256-policy", key_id=b"\x03", slot=token["slot"]), USER_PIN)


def test_signatures_survive_permit_tampering(token):
    signer = PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="test-policy", key_id=b"\x01", slot=token["slot"]), USER_PIN)
    permit = _permit()
    signature = signer.sign(permit)
    assert not signer.verify(_permit(amount_units=permit.amount_units + 1), signature)
    assert not signer.verify(_permit(recipient="0x4444444444444444444444444444444444444444"), signature)


def test_concurrent_signatures_stay_distinct_and_verifiable(token):
    """One signer, many threads: no shared session state may leak between calls.

    A cached session or object handle would be the natural optimisation here and the
    natural source of a cross-talk bug, so the signer is exercised under real concurrency.
    """
    from concurrent.futures import ThreadPoolExecutor

    signer = PKCS11PermitSigner(LIB, PKCS11KeyLocator(label="test-policy", key_id=b"\x01", slot=token["slot"]), USER_PIN)
    low = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141 // 2
    # One permit object per index, built once and reused: a permit carries an expiry, so
    # rebuilding it between signing and verifying would compare two different messages.
    permits = {index: _permit(payment_id="0x" + f"{index:064x}") for index in range(128)}

    def sign_once(index: int) -> str:
        signature = signer.sign(permits[index])
        assert signature[-2:] in ("1b", "1c")
        assert int(signature[66:130], 16) <= low
        return signature

    with ThreadPoolExecutor(max_workers=16) as executor:
        signatures = list(executor.map(sign_once, range(128)))
    assert len(set(signatures)) == 128
    # Every one of them must still recover to the token address, for its own permit only.
    for index, signature in enumerate(signatures):
        assert signer.verify(permits[index], signature)
    assert not signer.verify(permits[1], signatures[0])
    assert not signer.verify(permits[0], signatures[1])


def test_env_signer_control_still_verifies():
    """Control: the env backend still works, so a PKCS#11 failure is not a broken test."""
    signer = EIP712PermitSigner("0x" + "42" * 32)
    permit = _permit()
    assert signer.verify(permit, signer.sign(permit))
