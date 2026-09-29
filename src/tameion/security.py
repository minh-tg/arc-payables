from __future__ import annotations

from eth_account import Account
from eth_account.messages import encode_typed_data

from .domain import PaymentPermit


class SignerBackendUnavailable(RuntimeError):
    """Raised when the configured signing backend cannot produce permits."""


PERMIT_TYPES = {
    "Permit": [
        {"name": "payer", "type": "address"},
        {"name": "token", "type": "address"},
        {"name": "recipient", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "evidenceHash", "type": "bytes32"},
        {"name": "paymentId", "type": "bytes32"},
        {"name": "expiry", "type": "uint64"},
    ]
}


def permit_typed_message(permit: PaymentPermit):
    return encode_typed_data(
        domain_data={
            "name": "TameionPaymentGuard",
            "version": "1",
            "chainId": permit.chain_id,
            "verifyingContract": permit.guard_address,
        },
        message_types=PERMIT_TYPES,
        message_data=permit.as_message(),
    )


class EIP712PermitSigner:
    """Policy-only EIP-712 signer backed by an environment-provided key.

    This is the MVP backend. It is a `PermitSigner` implementation so an HSM/KMS-backed
    key can replace it without the authorization logic changing. The signing credential
    lives only inside the payment authorization service: `DecisionAgent` never receives a
    signer, and the workflow never places a signer or key inside agent context.
    """

    backend = "env"

    def __init__(self, private_key: str | bytes):
        self._private_key = private_key
        self.address = Account.from_key(private_key).address

    def sign(self, permit: PaymentPermit) -> str:
        message = permit_typed_message(permit)
        signed = Account.sign_message(message, private_key=self._private_key)
        return "0x" + bytes(signed.signature).hex()

    def verify(self, permit: PaymentPermit, signature: str) -> bool:
        try:
            recovered = Account.recover_message(permit_typed_message(permit), signature=signature)
            return recovered.lower() == self.address.lower()
        except (ValueError, TypeError):
            return False


def build_permit_signer(settings, private_key: str | None = None) -> "PermitSigner":
    """Construct the configured policy signer.

    `SIGNER_BACKEND=env` uses ``PERMIT_SIGNING_PRIVATE_KEY``. A `kms` backend is
    deliberately not faked: an unimplemented key backend must fail closed rather than
    silently fall back to a local key.
    """
    backend = (getattr(settings, "signer_backend", "env") or "env").lower()
    if backend == "kms":
        raise SignerBackendUnavailable(
            "The kms signer backend is not implemented. PERMIT_SIGNING_PRIVATE_KEY signing "
            "is the only MVP backend. Implement the PermitSigner interface against your KMS "
            "and set SIGNER_BACKEND=kms once it can sign EIP-712 permits."
        )
    if backend != "env":
        raise SignerBackendUnavailable(f"Unknown SIGNER_BACKEND {backend!r}; expected 'env' or 'kms'.")
    key = private_key or getattr(settings, "permit_signing_private_key", None)
    if not key:
        raise SignerBackendUnavailable("PERMIT_SIGNING_PRIVATE_KEY is required when SIGNER_BACKEND=env.")
    return EIP712PermitSigner(key)
