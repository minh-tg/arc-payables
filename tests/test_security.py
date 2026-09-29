from __future__ import annotations

import time

from eth_account import Account

from arc_payables.domain import PaymentPermit
from arc_payables.security import EIP712PermitSigner


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


def test_eip712_permit_signature_is_bound_to_all_material_fields():
    signer = EIP712PermitSigner("0x" + "42".zfill(64))
    permit = _permit()
    signature = signer.sign(permit)
    assert signer.verify(permit, signature)
    for changed in (
        _permit(recipient="0x4444444444444444444444444444444444444444"),
        _permit(amount_units=250_000_001),
        _permit(token="0x5555555555555555555555555555555555555555"),
        _permit(payment_id="0x" + "ef" * 32),
        _permit(evidence_hash="0x" + "ef" * 32),
        _permit(chain_id=1),
        _permit(guard_address="0x4444444444444444444444444444444444444444"),
    ):
        assert not signer.verify(changed, signature)


def test_eip712_invalid_signature_is_rejected():
    signer = EIP712PermitSigner("0x" + "42".zfill(64))
    other = EIP712PermitSigner(Account.create().key)
    permit = _permit()
    assert not signer.verify(permit, other.sign(permit))
    assert not signer.verify(permit, "not-a-signature")
