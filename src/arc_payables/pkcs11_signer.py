"""Policy-only EIP-712 signer backed by a PKCS#11 token (HSM or SoftHSM2 test double).

The private key never leaves the token: signing happens inside it via C_Sign over a
precomputed 32-byte digest with CKM_ECDSA (raw r||s), and the Ethereum signature is
assembled locally from (r, s) plus a recovery id derived by trial recovery against the
token's own public key. Low-s is enforced (flipping s flips the recovery parity), so the
guard's malleability check and eth-account signatures verify identically.

SoftHSM2 is a software-backed behavior double for API/integration tests, not a hardware
security boundary. Production must use a real HSM on an isolated host; see docs/signing.md.
"""

from __future__ import annotations

import ctypes
import os
import threading
from dataclasses import dataclass
from typing import Any

from eth_hash.auto import keccak
from eth_keys import keys

from .domain import PaymentPermit
from .security import SignerBackendUnavailable, permit_typed_message


SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class PKCS11Error(SignerBackendUnavailable):
    """The PKCS#11 token is unreachable, misconfigured, or refused the operation."""


@dataclass(frozen=True)
class PKCS11KeyLocator:
    """How to find the signing key on the token. No secrets: only object identity."""

    label: str
    key_id: bytes = b"\x01"
    slot: int | None = None


# -- Minimal PKCS#11 (Cryptoki) ctypes binding -------------------------------------
# Only what signing needs: open a read-only session, log in, find one EC private key
# plus its public half, and C_Sign with CKM_ECDSA. Anything else fails closed.

_CKR_OK = 0x00000000
_CKR_ARGUMENTS_BAD = 0x00000005
_CKR_CANT_LOCK = 0x0000000A
_CKR_CRYPTOKI_ALREADY_INITIALIZED = 0x00000191
_CKR_USER_ALREADY_LOGGED_IN = 0x00000100
_CKU_USER = 1
_CKO_PRIVATE_KEY = 3
_CKO_PUBLIC_KEY = 2
_CKK_EC = 3
_CKM_ECDSA = 0x00001041
_CKA_CLASS = 0x00000000
_CKA_LABEL = 0x00000003
_CKA_ID = 0x00000102
_CKA_KEY_TYPE = 0x00000100
_CKA_EC_PARAMS = 0x00000180
_CKA_EC_POINT = 0x00000181
_CKA_VALUE = 0x00000111
_CKA_SENSITIVE = 0x00000103
_CKA_EXTRACTABLE = 0x00000162
_CKA_SIGN = 0x00000108
_CKF_SERIAL_SESSION = 0x00000004
_CKF_OS_LOCKING_OK = 0x00000002
_CK_UNAVAILABLE_INFORMATION = 0xFFFFFFFF
_CK_TRUE = 1

_CK_RV = ctypes.c_ulong
_CK_ULONG = ctypes.c_ulong
_CK_SLOT_ID = ctypes.c_ulong
_CK_SESSION_HANDLE = ctypes.c_ulong
_CK_OBJECT_HANDLE = ctypes.c_ulong
_CK_OBJECT_CLASS = ctypes.c_ulong
_CK_KEY_TYPE = ctypes.c_ulong


class _Attribute(ctypes.Structure):
    _fields_ = [("type", _CK_ULONG), ("pValue", ctypes.c_void_p), ("ulValueLen", _CK_ULONG)]


class _Mechanism(ctypes.Structure):
    _fields_ = [("mechanism", _CK_ULONG), ("pParameter", ctypes.c_void_p), ("ulParameterLen", _CK_ULONG)]


class _InitializeArgs(ctypes.Structure):
    """CK_C_INITIALIZE_ARGS. Declaring CKF_OS_LOCKING_OK is what makes concurrent use safe.

    Without it a PKCS#11 library may assume the *application* serialises every call, and
    calling C_Sign from several threads then corrupts memory instead of failing cleanly.
    """

    _fields_ = [
        ("CreateMutex", ctypes.c_void_p),
        ("DestroyMutex", ctypes.c_void_p),
        ("LockMutex", ctypes.c_void_p),
        ("UnlockMutex", ctypes.c_void_p),
        ("flags", _CK_ULONG),
        ("pReserved", ctypes.c_void_p),
    ]


def _rv(call: str, code: int) -> None:
    if code != _CKR_OK:
        raise PKCS11Error(f"PKCS#11 {call} failed (rv=0x{code:08x}); refusing to sign.")


_LIBRARIES: dict[str, Any] = {}
_LIBRARIES_LOCK = threading.Lock()
#: Serializes PKCS#11 calls in this process. Held even when the library declares
#: ``CKF_OS_LOCKING_OK``, so correctness never depends on which mode the library accepted.
_CALL_LOCK = threading.RLock()


def _load_library(path: str):
    """Load and initialize a PKCS#11 library once per process, under a lock.

    Repeatedly calling C_Initialize from many threads, or racing the first load, is a
    documented misuse of Cryptoki. Caching the handle keeps initialization single and
    lets every later session share the same already-initialized library.
    """
    with _LIBRARIES_LOCK:
        cached = _LIBRARIES.get(path)
        if cached is not None:
            return cached
        loader = _open_library(path)
        _LIBRARIES[path] = loader
        return loader


def _open_library(path: str):
    try:
        loader = ctypes.CDLL(path)
    except OSError as exc:
        raise PKCS11Error(f"PKCS#11 library {path!r} could not be loaded; refusing to sign.") from exc
    loader.C_Initialize.argtypes = [ctypes.c_void_p]
    loader.C_Initialize.restype = _CK_RV
    loader.C_GetSlotList.argtypes = [ctypes.c_ubyte, ctypes.POINTER(_CK_SLOT_ID), ctypes.POINTER(_CK_ULONG)]
    loader.C_GetSlotList.restype = _CK_RV
    loader.C_OpenSession.argtypes = [_CK_SLOT_ID, _CK_ULONG, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_CK_SESSION_HANDLE)]
    loader.C_OpenSession.restype = _CK_RV
    loader.C_Login.argtypes = [_CK_SESSION_HANDLE, _CK_ULONG, ctypes.c_char_p, _CK_ULONG]
    loader.C_Login.restype = _CK_RV
    loader.C_Logout.argtypes = [_CK_SESSION_HANDLE]
    loader.C_Logout.restype = _CK_RV
    loader.C_CloseSession.argtypes = [_CK_SESSION_HANDLE]
    loader.C_CloseSession.restype = _CK_RV
    loader.C_FindObjectsInit.argtypes = [_CK_SESSION_HANDLE, ctypes.POINTER(_Attribute), _CK_ULONG]
    loader.C_FindObjectsInit.restype = _CK_RV
    loader.C_FindObjects.argtypes = [_CK_SESSION_HANDLE, ctypes.POINTER(_CK_OBJECT_HANDLE), _CK_ULONG, ctypes.POINTER(_CK_ULONG)]
    loader.C_FindObjects.restype = _CK_RV
    loader.C_FindObjectsFinal.argtypes = [_CK_SESSION_HANDLE]
    loader.C_FindObjectsFinal.restype = _CK_RV
    loader.C_GetAttributeValue.argtypes = [_CK_SESSION_HANDLE, _CK_OBJECT_HANDLE, ctypes.POINTER(_Attribute), _CK_ULONG]
    loader.C_GetAttributeValue.restype = _CK_RV
    loader.C_SignInit.argtypes = [_CK_SESSION_HANDLE, ctypes.POINTER(_Mechanism), _CK_OBJECT_HANDLE]
    loader.C_SignInit.restype = _CK_RV
    loader.C_Sign.argtypes = [_CK_SESSION_HANDLE, ctypes.c_char_p, _CK_ULONG, ctypes.c_char_p, ctypes.POINTER(_CK_ULONG)]
    loader.C_Sign.restype = _CK_RV
    _initialize(loader)
    return loader


def _initialize(loader: Any) -> None:
    """Initialize the library, preferring OS-level locking.

    ``CKF_OS_LOCKING_OK`` asks the library to lock internally, which is what the spec requires
    before several threads may call it. Not every build accepts the argument structure: an older
    SoftHSM rejects it with ``CKR_ARGUMENTS_BAD``. In that case fall back to ``C_Initialize(NULL)``,
    which claims nothing about thread safety, and rely on ``_CALL_LOCK`` to serialize every call
    ourselves. Anything else fails closed: an unusable library is never used unsafely.
    """
    for flags in (_CKF_OS_LOCKING_OK, 0):
        args = _InitializeArgs(None, None, None, None, flags, None)
        code = loader.C_Initialize(ctypes.byref(args))
        if code in (_CKR_OK, _CKR_CRYPTOKI_ALREADY_INITIALIZED):
            return
        if code not in (_CKR_ARGUMENTS_BAD, _CKR_CANT_LOCK):
            break
    else:
        code = loader.C_Initialize(None)
        if code in (_CKR_OK, _CKR_CRYPTOKI_ALREADY_INITIALIZED):
            return
    raise PKCS11Error(
        f"PKCS#11 C_Initialize failed for {getattr(loader, '_name', None) or 'the configured library'} "
        f"(rv=0x{code:08x}); refusing to sign. A CKR_ARGUMENTS_BAD (0x5) here usually means the "
        f"library could not load its own configuration: for SoftHSM, set SOFTHSM2_CONF to a valid "
        f"config whose token directory exists, and confirm PKCS11_LIB_PATH names the provider "
        f"library rather than a p11-kit module proxy."
    )


def _template(entries: list[tuple[int, bytes | None]]) -> tuple[list, list]:
    """Build a CK_ATTRIBUTE array. None means query-only (length unknown)."""
    kept: list = []
    attrs: list = []
    for kind, value in entries:
        attr = _Attribute()
        attr.type = kind
        if value is None:
            attr.pValue = None
            attr.ulValueLen = 0
        else:
            buffer = ctypes.create_string_buffer(bytes(value))
            kept.append(buffer)
            attr.pValue = ctypes.cast(buffer, ctypes.c_void_p)
            attr.ulValueLen = len(value)
        attrs.append(attr)
    array = (_Attribute * len(attrs))(*attrs)
    return kept, array


class _Session:
    """One logged-in read-only PKCS#11 session. Closed explicitly; never shared."""

    def __init__(self, lib_path: str, slot: int | None, user_pin: str):
        with _CALL_LOCK:
            self._lib = _load_library(lib_path)
            count = _CK_ULONG(0)
            _rv("C_GetSlotList", self._lib.C_GetSlotList(1, None, ctypes.byref(count)))
            if count.value == 0:
                raise PKCS11Error("PKCS#11 token reports no slots with a token present; refusing to sign.")
            slots = (_CK_SLOT_ID * count.value)()
            _rv("C_GetSlotList", self._lib.C_GetSlotList(1, slots, ctypes.byref(count)))
            available = [slots[i] for i in range(count.value)]
            if slot is not None and slot not in available:
                raise PKCS11Error("PKCS#11 configured slot has no token present; refusing to sign.")
            self._slot = slot if slot is not None else available[0]
            if slot is None and len(available) > 1:
                raise PKCS11Error("PKCS#11 found several tokens; set PKCS11_SLOT to pin one. Refusing to guess.")
            handle = _CK_SESSION_HANDLE(0)
            _rv("C_OpenSession", self._lib.C_OpenSession(self._slot, _CKF_SERIAL_SESSION, None, None, ctypes.byref(handle)))
            self.handle = handle.value
            self._open = True
            try:
                # Login is token-wide, not per-session: a second concurrent signer legitimately
                # gets CKR_USER_ALREADY_LOGGED_IN, which is success, not a failure.
                code = self._lib.C_Login(self.handle, _CKU_USER, user_pin.encode(), len(user_pin.encode()))
                if code not in (_CKR_OK, _CKR_USER_ALREADY_LOGGED_IN):
                    raise PKCS11Error(f"PKCS#11 C_Login failed (rv=0x{code:08x}); refusing to sign.")
            except Exception:
                self.close()
                raise

    def find(self, template_entries: list[tuple[int, bytes | None]]) -> list[int]:
        with _CALL_LOCK:
            kept, array = _template(template_entries)
            _rv("C_FindObjectsInit", self._lib.C_FindObjectsInit(self.handle, array, len(template_entries)))
            try:
                found = (_CK_OBJECT_HANDLE * 8)()
                total = _CK_ULONG(0)
                _rv("C_FindObjects", self._lib.C_FindObjects(self.handle, found, 8, ctypes.byref(total)))
                return [found[i] for i in range(total.value)]
            finally:
                _rv("C_FindObjectsFinal", self._lib.C_FindObjectsFinal(self.handle))

    def get_bytes(self, handle: int, kind: int) -> bytes:
        with _CALL_LOCK:
            kept, array = _template([(kind, None)])
            _rv("C_GetAttributeValue", self._lib.C_GetAttributeValue(self.handle, handle, array, 1))
            # A sensitive/unextractable attribute must come back as unavailable, never as bytes.
            if array[0].ulValueLen == _CK_UNAVAILABLE_INFORMATION:
                raise PKCS11Error("PKCS#11 refused to reveal a private attribute; refusing to sign.")
            length = array[0].ulValueLen
            if length == 0 or length > 4096:
                raise PKCS11Error("PKCS#11 returned an unusable attribute length; refusing to sign.")
            buffer = ctypes.create_string_buffer(length)
            query = _Attribute()
            query.type = kind
            query.pValue = ctypes.cast(buffer, ctypes.c_void_p)
            query.ulValueLen = length
            _rv("C_GetAttributeValue", self._lib.C_GetAttributeValue(self.handle, handle, ctypes.byref(query), 1))
            return bytes(buffer.raw)

    def get_flag(self, handle: int, kind: int) -> bool:
        raw = self.get_bytes(handle, kind)
        if len(raw) != 1:
            raise PKCS11Error("PKCS#11 boolean attribute has an unexpected size; refusing to sign.")
        return raw[0] == _CK_TRUE

    def sign_raw_ecdsa(self, key_handle: int, digest: bytes) -> bytes:
        if len(digest) != 32:
            raise ValueError("PKCS#11 signs a precomputed 32-byte digest, nothing else.")
        with _CALL_LOCK:
            mechanism = _Mechanism(_CKM_ECDSA, None, 0)
            _rv("C_SignInit", self._lib.C_SignInit(self.handle, ctypes.byref(mechanism), key_handle))
            out = ctypes.create_string_buffer(128)
            out_len = _CK_ULONG(128)
            _rv("C_Sign", self._lib.C_Sign(self.handle, digest, len(digest), out, ctypes.byref(out_len)))
            if out_len.value != 64:
                raise PKCS11Error("PKCS#11 ECDSA signature is not raw r||s; refusing to sign.")
            return bytes(out.raw[:64])

    def close(self) -> None:
        """Close the session only.

        Deliberately no C_Logout: login is token-wide, so logging out here would pull the
        token out from under a concurrent signer's already-authenticated session. Cryptoki
        logs the user out when the last session closes, so an idle process does not leave
        the token logged in.
        """
        if not self._open:
            return
        self._open = False
        with _CALL_LOCK:
            try:
                self._lib.C_CloseSession(self.handle)
            except Exception:
                pass


SECP256K1_PARAMS_HEX = "06052b8104000a"  # OID 1.3.132.0.10


def _uncompressed_point(ec_point_attr: bytes) -> bytes:
    """Unwrap the DER OCTET STRING around the 65-byte uncompressed point."""
    if len(ec_point_attr) != 67 or ec_point_attr[0] != 0x04 or ec_point_attr[1] != 0x41:
        raise PKCS11Error("PKCS#11 EC_POINT is not a 65-byte uncompressed point; refusing to sign.")
    point = ec_point_attr[2:]
    if point[0] != 0x04 or len(point) != 65:
        raise PKCS11Error("PKCS#11 public point is malformed; refusing to sign.")
    return point


def _check_private_key(session: _Session, handle: int, locator: PKCS11KeyLocator) -> None:
    """Prove the key is a sign-only, on-token, non-extractable secp256k1 key."""
    params = session.get_bytes(handle, _CKA_EC_PARAMS)
    if params.hex() != SECP256K1_PARAMS_HEX:
        raise PKCS11Error("PKCS#11 key is not secp256k1; refusing to sign.")
    if not session.get_flag(handle, _CKA_SIGN):
        raise PKCS11Error(f"PKCS#11 key {locator.label!r} cannot sign; refusing to sign.")
    if not session.get_flag(handle, _CKA_SENSITIVE):
        raise PKCS11Error(f"PKCS#11 key {locator.label!r} is not sensitive; refusing to sign.")
    if session.get_flag(handle, _CKA_EXTRACTABLE):
        raise PKCS11Error(f"PKCS#11 key {locator.label!r} is extractable; refusing to sign.")
    try:
        session.get_bytes(handle, _CKA_VALUE)
    except PKCS11Error:
        pass  # CKA_VALUE correctly unavailable: the scalar cannot be read. Expected.
    else:
        raise PKCS11Error(f"PKCS#11 key {locator.label!r} exposed its private value; refusing to sign.")


def _recover_parity(digest: bytes, r: int, s: int, address: str) -> int:
    """Return the y-parity (0/1) whose recovery matches the token's public key."""
    for parity in (0, 1):
        try:
            recovered = keys.Signature(vrs=(parity, r, s)).recover_public_key_from_msg_hash(digest)
        except Exception:
            continue
        if recovered.to_checksum_address().lower() == address.lower():
            return parity
    raise PKCS11Error("PKCS#11 signature does not recover to the token key; refusing to sign.")


def _assemble_eth_signature(digest: bytes, raw: bytes, address: str) -> str:
    r = int.from_bytes(raw[:32], "big")
    s = int.from_bytes(raw[32:], "big")
    if not (1 <= r < SECP256K1_N and 1 <= s < SECP256K1_N):
        raise PKCS11Error("PKCS#11 signature has out-of-range r/s; refusing to sign.")
    if s > SECP256K1_N // 2:
        s = SECP256K1_N - s  # low-s: the guard rejects malleable high-s signatures
    # Parity is re-derived against the canonical low-s encoded below, so the s-flip
    # cannot desynchronise the recovery id from the (r, s) actually encoded.
    parity = _recover_parity(digest, r, s, address)
    return "0x" + r.to_bytes(32, "big").hex() + s.to_bytes(32, "big").hex() + (27 + parity).to_bytes(1, "big").hex()


def _eip712_digest(permit: PaymentPermit) -> bytes:
    message = permit_typed_message(permit)
    version = bytes(message.version) if not isinstance(message.version, int) else bytes([message.version])
    return keccak(b"\x19" + version + bytes(message.header) + bytes(message.body))


def _defunct_digest(digest: bytes) -> bytes:
    return keccak(b"\x19Ethereum Signed Message:\n32" + digest)


class PKCS11PermitSigner:
    """Policy-only signer whose key lives on a PKCS#11 token and cannot be exported.

    One short-lived session per signature: no cached handles, no background threads.
    Thread-safe by construction, since no session state is shared between calls.
    """

    backend = "pkcs11"

    def __init__(self, lib_path: str, locator: PKCS11KeyLocator, user_pin: str):
        if not lib_path or not os.path.exists(lib_path):
            raise PKCS11Error("PKCS11_LIB_PATH must name a readable PKCS#11 library; refusing to sign.")
        if not locator.label:
            raise PKCS11Error("PKCS11_KEY_LABEL must name the signing key; refusing to sign.")
        if not user_pin:
            raise PKCS11Error("A PKCS#11 user PIN is required; refusing to sign.")
        self._lib_path = lib_path
        self._locator = locator
        self._pin = user_pin
        self.address = self._load_address()

    def _session(self) -> _Session:
        return _Session(self._lib_path, self._locator.slot, self._pin)

    def _key_handles(self, session: _Session) -> tuple[int, int]:
        label = self._locator.label.encode()
        private = session.find([
            (_CKA_CLASS, bytes(_CK_OBJECT_CLASS(_CKO_PRIVATE_KEY))),
            (_CKA_KEY_TYPE, bytes(_CK_KEY_TYPE(_CKK_EC))),
            (_CKA_LABEL, label),
            (_CKA_ID, self._locator.key_id),
        ])
        public = session.find([
            (_CKA_CLASS, bytes(_CK_OBJECT_CLASS(_CKO_PUBLIC_KEY))),
            (_CKA_KEY_TYPE, bytes(_CK_KEY_TYPE(_CKK_EC))),
            (_CKA_LABEL, label),
            (_CKA_ID, self._locator.key_id),
        ])
        if len(private) != 1 or len(public) != 1:
            raise PKCS11Error(f"PKCS#11 key {self._locator.label!r} is missing or ambiguous; refusing to sign.")
        return private[0], public[0]

    def _load_address(self) -> str:
        session = self._session()
        try:
            private_handle, public_handle = self._key_handles(session)
            _check_private_key(session, private_handle, self._locator)
            point = _uncompressed_point(session.get_bytes(public_handle, _CKA_EC_POINT))
            return keys.PublicKey(point[1:]).to_checksum_address()
        finally:
            session.close()

    def _sign_digest(self, digest: bytes, eip712: bool) -> str:
        if len(digest) != 32:
            raise ValueError("PKCS#11 signs a precomputed 32-byte digest, nothing else.")
        session = self._session()
        try:
            private_handle, _ = self._key_handles(session)
            _check_private_key(session, private_handle, self._locator)
            material = digest if eip712 else _defunct_digest(digest)
            raw = session.sign_raw_ecdsa(private_handle, material)
            return _assemble_eth_signature(material, raw, self.address)
        finally:
            session.close()

    def sign(self, permit: PaymentPermit) -> str:
        return self._sign_digest(_eip712_digest(permit), True)

    def verify(self, permit: PaymentPermit, signature: str) -> bool:
        try:
            from eth_account import Account

            recovered = Account.recover_message(permit_typed_message(permit), signature=signature)
            return recovered.lower() == self.address.lower()
        except (ValueError, TypeError):
            return False

    def sign_digest(self, digest: bytes) -> str:
        """Sign a 32-byte audit digest with the same non-exportable key."""
        return self._sign_digest(bytes(digest), False)


def pkcs11_settings_present(settings: Any) -> bool:
    return bool(
        getattr(settings, "pkcs11_lib_path", None)
        and getattr(settings, "pkcs11_key_label", None)
        and getattr(settings, "pkcs11_user_pin", None)
    )


def build_pkcs11_signer(settings: Any) -> PKCS11PermitSigner:
    """Construct the PKCS#11 policy signer from explicit, validated configuration."""
    lib_path = (getattr(settings, "pkcs11_lib_path", None) or "").strip()
    label = (getattr(settings, "pkcs11_key_label", None) or "").strip()
    pin = getattr(settings, "pkcs11_user_pin", None) or ""
    key_id_hex = (getattr(settings, "pkcs11_key_id_hex", None) or "01").strip()
    slot = getattr(settings, "pkcs11_slot", None)
    try:
        key_id = bytes.fromhex(key_id_hex)
    except ValueError as exc:
        raise PKCS11Error("PKCS11_KEY_ID_HEX must be even-length hex; refusing to sign.") from exc
    if not key_id or len(key_id) > 64:
        raise PKCS11Error("PKCS11_KEY_ID_HEX must name a non-empty key id; refusing to sign.")
    if getattr(settings, "pkcs11_test_backend", False):
        raise PKCS11Error("PKCS11_TEST_BACKEND is a development-only SoftHSM2 marker; production refuses it outright.")
    signer = PKCS11PermitSigner(lib_path, PKCS11KeyLocator(label=label, key_id=key_id, slot=slot), pin)
    from .security import _check_pinned_address

    _check_pinned_address(settings, signer.address)
    return signer
