from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak

from .domain import PaymentPermit

PERMIT_ABI_SIGNATURE = "pay((address,address,address,uint256,bytes32,bytes32,uint64),bytes)"


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def bytes32(value: str | bytes) -> bytes:
    """Normalise a 32-byte value for ABI encoding.

    Permit hashes and payment ids travel as hex strings, but eth_abi requires raw bytes for
    ``bytes32``. Passing the string straight through raises EncodingTypeError at submission
    time, so encode it here where every caller gets the same treatment.
    """
    if isinstance(value, bytes):
        raw = value
    else:
        raw = bytes.fromhex(value.removeprefix("0x"))
    if len(raw) != 32:
        raise ValueError(f"expected 32 bytes, got {len(raw)}")
    return raw


def encode_permit_call(permit: PaymentPermit, signature: str) -> str:
    signature_bytes = bytes.fromhex(signature.removeprefix("0x"))
    values = (
        permit.payer,
        permit.token,
        permit.recipient,
        permit.amount_units,
        bytes32(permit.evidence_hash),
        bytes32(permit.payment_id),
        permit.expiry,
    )
    payload = selector(PERMIT_ABI_SIGNATURE) + encode(
        ["(address,address,address,uint256,bytes32,bytes32,uint64)", "bytes"],
        [values, signature_bytes],
    )
    return "0x" + payload.hex()


def encode_approve(spender: str, amount: int) -> str:
    return "0x" + (selector("approve(address,uint256)") + encode(["address", "uint256"], [spender, amount])).hex()


def encode_allowance(owner: str, spender: str) -> str:
    return "0x" + (selector("allowance(address,address)") + encode(["address", "address"], [owner, spender])).hex()


def encode_balance_of(owner: str) -> str:
    return "0x" + (selector("balanceOf(address)") + encode(["address"], [owner])).hex()


def encode_used(payment_id: str) -> str:
    return "0x" + (selector("used(bytes32)") + encode(["bytes32"], [bytes32(payment_id)])).hex()


def decode_uint256(result: str) -> int:
    return int(decode(["uint256"], bytes.fromhex(result.removeprefix("0x")))[0])


def decode_bool(result: str) -> bool:
    return bool(decode(["bool"], bytes.fromhex(result.removeprefix("0x")))[0])
