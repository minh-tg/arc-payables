from __future__ import annotations

import base64

from Crypto.Cipher import PKCS1_OAEP
from Crypto.Hash import SHA256
from Crypto.PublicKey import RSA


def encrypt_circle_entity_secret(entity_secret_hex: str, public_key_pem: str) -> str:
    """Circle's documented RSA-OAEP SHA-256 entity-secret encryption flow."""
    try:
        secret = bytes.fromhex(entity_secret_hex.removeprefix("0x"))
    except ValueError as exc:
        raise ValueError("Circle entity secret must be 64 hexadecimal characters") from exc
    if len(secret) != 32:
        raise ValueError("Circle entity secret must be exactly 32 bytes")
    key = RSA.import_key(public_key_pem)
    cipher = PKCS1_OAEP.new(key=key, hashAlgo=SHA256)
    return base64.b64encode(cipher.encrypt(secret)).decode("ascii")
