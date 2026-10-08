"""Hermetic managed-signing checks that need no token and no SoftHSM2.

These run in the default suite. The token-backed tests live in test_pkcs11_signer.py
and are opt-in, because they need a real PKCS#11 library and tools.
"""

from __future__ import annotations

import ctypes
import time

import pytest

from arc_payables.domain import PaymentPermit
from arc_payables.pkcs11_signer import PKCS11Error, _InitializeArgs, _initialize
from arc_payables.security import EIP712PermitSigner, SignerBackendUnavailable, build_permit_signer
from arc_payables.settings import Settings


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


class _FakeLoader:
    """Records how C_Initialize was called and returns scripted return codes."""

    def __init__(self, codes: list[int]):
        self.codes = list(codes)
        self.calls: list[str] = []

    def C_Initialize(self, arg):
        if arg is None:
            self.calls.append("null")
        else:
            # The init args struct is passed by reference; read its flags field back.
            struct = ctypes.cast(arg, ctypes.POINTER(_InitializeArgs)).contents
            self.calls.append("flags=OS_LOCKING" if struct.flags else "flags=0")
        return self.codes.pop(0) if self.codes else 0


def test_initialize_prefers_os_locking_and_falls_back_when_refused():
    """Some builds reject the OS-locking argument structure, so the ladder must degrade.

    CI found this: Ubuntu's SoftHSM returns CKR_ARGUMENTS_BAD (0x5) for the args struct, and
    signing then refused to start at all. It must retry with flags=0 and then with NULL
    (safe because every PKCS#11 call is serialised in-process), while any other failure
    stays closed and is not retried.
    """
    preferred = _FakeLoader([0])
    _initialize(preferred)
    assert preferred.calls == ["flags=OS_LOCKING"]

    already = _FakeLoader([0x00000191])  # CKR_CRYPTOKI_ALREADY_INITIALIZED
    _initialize(already)
    assert already.calls == ["flags=OS_LOCKING"]

    # OS-locking rejected, bare args accepted.
    bare = _FakeLoader([0x00000005, 0])  # CKR_ARGUMENTS_BAD, then OK
    _initialize(bare)
    assert bare.calls == ["flags=OS_LOCKING", "flags=0"]

    # Both argument forms rejected, so NULL is tried last.
    nulled = _FakeLoader([0x00000005, 0x0000000A, 0x00000191])  # ARGUMENTS_BAD, CANT_LOCK, already
    _initialize(nulled)
    assert nulled.calls == ["flags=OS_LOCKING", "flags=0", "null"]

    # Nothing works: refuse, and never pretend the library is usable.
    hopeless = _FakeLoader([0x00000005, 0x00000005, 0x00000007])
    with pytest.raises(PKCS11Error, match="C_Initialize failed"):
        _initialize(hopeless)
    assert hopeless.calls == ["flags=OS_LOCKING", "flags=0", "null"]

    # An unrelated failure is not retried at all.
    broken = _FakeLoader([0x00000007])
    with pytest.raises(PKCS11Error, match="C_Initialize failed"):
        _initialize(broken)
    assert broken.calls == ["flags=OS_LOCKING"]

    # A rejected second attempt that is not a locking complaint also stops immediately.
    odd = _FakeLoader([0x00000005, 0x00000007])
    with pytest.raises(PKCS11Error, match="C_Initialize failed"):
        _initialize(odd)
    assert odd.calls == ["flags=OS_LOCKING", "flags=0"]


def test_pkcs11_factory_fails_closed_without_a_usable_library(tmp_path):
    """Misconfiguration must refuse rather than fall back to an environment key."""
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_LIB_PATH"):
        build_permit_signer(Settings(_env_file=None, signer_backend="pkcs11"))
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_LIB_PATH"):
        build_permit_signer(
            Settings(_env_file=None, signer_backend="pkcs11", pkcs11_lib_path="/nonexistent/lib.so",
                     pkcs11_key_label="policy-sign", pkcs11_user_pin="1234")
        )
    # A readable path is needed to reach the later checks; the file is not a real library,
    # which is exactly what must be reported rather than silently ignored.
    present = tmp_path / "not-a-library.so"
    present.write_bytes(b"")
    with pytest.raises(SignerBackendUnavailable, match="PKCS11_KEY_LABEL"):
        build_permit_signer(
            Settings(_env_file=None, signer_backend="pkcs11", pkcs11_lib_path=str(present))
        )
    with pytest.raises(SignerBackendUnavailable, match="development-only"):
        build_permit_signer(
            Settings(_env_file=None, signer_backend="pkcs11", pkcs11_lib_path=str(present),
                     pkcs11_key_label="policy-sign", pkcs11_user_pin="1234", pkcs11_test_backend=True)
        )
    # A file that is not a loadable PKCS#11 library must fail closed, not be ignored.
    with pytest.raises(Exception):
        build_permit_signer(
            Settings(_env_file=None, signer_backend="pkcs11", pkcs11_lib_path=str(present),
                     pkcs11_key_label="policy-sign", pkcs11_user_pin="1234")
        )


def test_pkcs11_credentials_are_not_serialized_by_settings():
    settings = Settings(
        _env_file=None,
        signer_backend="pkcs11",
        pkcs11_lib_path="/usr/lib/libsofthsm2.so",
        pkcs11_key_label="policy-sign",
        pkcs11_user_pin="pin-that-must-never-be-reprised",
    )
    assert "pin-that-must-never-be-reprised" not in repr(settings)
    assert settings.policy_signing_configured is True
    assert Settings(_env_file=None, signer_backend="pkcs11").policy_signing_configured is False
    assert Settings(_env_file=None, permit_signing_private_key="0x" + "11" * 32).policy_signing_configured is True
    assert Settings(_env_file=None).policy_signing_configured is False
    assert Settings(_env_file=None, signer_backend="kms").policy_signing_configured is False


def test_env_signer_signs_and_verifies_the_permit_it_signed():
    """Control case: the env backend still behaves, so PKCS#11 differences are meaningful."""
    signer = EIP712PermitSigner("0x" + "42" * 32)
    permit = _permit()
    signature = signer.sign(permit)
    assert signer.verify(permit, signature)
    assert not signer.verify(_permit(amount_units=permit.amount_units + 1), signature)
    assert signer.sign_digest(b"\x11" * 32).startswith("0x")
