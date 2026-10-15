"""The Circle notification receiver: raw-body verification, one-time claim, and no money path.

Every test generates its own key pair and signs the exact bytes it posts, so no network and no real
Circle key is involved. The negative assertions carry most of the weight: an unverified or replayed
notification, or one naming no pending payment, must leave the payment record and its state exactly
as they were, and must never reach the provider's submit path.
"""

from __future__ import annotations

import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.circle_adapter import CircleNotificationKeyUnavailable
from arc_payables.domain import WorkflowState
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

KEY_ID = "key-2026-10"
TRANSACTION = "circle-operation-1"


def _keypair():
    key = ec.generate_private_key(ec.SECP256K1())
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return key, base64.b64encode(der).decode()


def _sign(key, body: bytes) -> str:
    return base64.b64encode(key.sign(body, ec.ECDSA(hashes.SHA256()))).decode()


class _Provider(MockPaymentProvider):
    """A provider that records any submit attempt instead of making it."""

    def __init__(self, store):
        super().__init__(store)
        self.submits = 0

    def submit_authorized(self, payment, on_transaction=None):
        self.submits += 1
        raise AssertionError("the webhook must never submit a payment")


def _payload(event: str = "TRANSACTION_CONFIRMED", notification_id="n-1", transaction_id=TRANSACTION) -> dict:
    body = {"notificationType": event, "notificationId": notification_id}
    if transaction_id is not None:
        body["transaction"] = {"id": transaction_id, "state": "CONFIRMED"}
    return body


def _app(tmp_path, *, resolver, settings_overrides=None):
    settings = Settings(
        _env_file=None,
        database_path=tmp_path / "webhook.sqlite3",
        payment_provider="circle",
        **(settings_overrides or {}),
    )
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    invoice_id, _ = seed_demo(store)
    provider = _Provider(store)
    app = create_app(
        settings=settings,
        store=store,
        accounting=MockAccountingConnector(store),
        payment_provider=provider,
        webhook_key_resolver=resolver,
    )
    return TestClient(app), store, provider, invoice_id


def _pending_payment(store, invoice_id, transaction_id=TRANSACTION, state=WorkflowState.SUBMITTED.value) -> dict:
    payment = {
        "payment_id": "pay-" + uuid.uuid4().hex[:12],
        "idempotency_key": str(uuid.uuid4()),
        "state": state,
        "confirmation_status": "PENDING",
        "provider_transaction_id": transaction_id,
        "permit": {"payment_id": "permit-1", "evidence_hash": "0x" + "ab" * 32},
        "decision_evidence_hash": "0x" + "ab" * 32,
    }
    store.create_payment(invoice_id, payment)
    return payment


def _post(client, key, body: bytes, *, key_id=KEY_ID, signature=None):
    return client.post(
        "/webhooks/circle",
        content=body,
        headers={
            "X-Circle-Key-Id": key_id,
            "X-Circle-Signature": signature if signature is not None else _sign(key, body),
            "Content-Type": "application/json",
        },
    )


def test_a_verified_notification_for_a_pending_payment_is_recorded_once(tmp_path):
    key, public_key = _keypair()
    client, store, provider, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    payment = _pending_payment(store, invoice_id)
    body = json.dumps(_payload()).encode()

    response = _post(client, key, body)

    assert response.status_code == 200
    assert response.json() == {"status": "recorded"}
    assert provider.submits == 0
    assert store.get_payment(invoice_id) == payment, "the webhook changed the payment record"
    assert store.get_state(invoice_id) == WorkflowState.AUTHORIZED.value
    assert store.claim_circle_notification("n-1", KEY_ID, "TRANSACTION_CONFIRMED", "recorded") is False


def test_a_replayed_notification_id_is_a_no_op_success(tmp_path):
    key, public_key = _keypair()
    client, store, _, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    _pending_payment(store, invoice_id)
    body = json.dumps(_payload()).encode()

    assert _post(client, key, body).json() == {"status": "recorded"}
    duplicate = _post(client, key, body)

    assert duplicate.status_code == 200
    assert duplicate.json() == {"status": "duplicate"}


def test_a_mutated_body_fails_verification(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = json.dumps(_payload()).encode()
    signature = _sign(key, body)

    response = _post(client, key, json.dumps(_payload(transaction_id="other")).encode(), signature=signature)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"
    assert signature not in response.text


def test_an_unknown_key_id_is_the_same_answer_as_a_bad_signature(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key if key_id == KEY_ID else None)
    body = json.dumps(_payload()).encode()

    response = _post(client, key, body, key_id="unknown-key")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_signature"


def test_a_signed_but_unhandled_type_is_recorded_and_ignored(tmp_path):
    key, public_key = _keypair()
    client, store, _, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    payment = _pending_payment(store, invoice_id)
    body = json.dumps(_payload(event="WALLET_CREATED")).encode()

    response = _post(client, key, body)

    assert response.status_code == 200
    assert response.json() == {"status": "ignored"}
    assert store.get_payment(invoice_id) == payment


def test_a_notification_for_no_pending_payment_is_unmatched(tmp_path):
    key, public_key = _keypair()
    client, store, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = json.dumps(_payload(transaction_id="no-such-operation")).encode()

    response = _post(client, key, body)

    assert response.status_code == 200
    assert response.json() == {"status": "unmatched"}


def test_a_notification_for_an_already_settled_payment_is_unmatched(tmp_path):
    key, public_key = _keypair()
    client, store, _, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    _pending_payment(store, invoice_id, state=WorkflowState.CONFIRMED.value)
    body = json.dumps(_payload()).encode()

    assert _post(client, key, body).json() == {"status": "unmatched"}


def test_missing_signature_headers_are_refused(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = json.dumps(_payload()).encode()

    response = client.post("/webhooks/circle", content=body, headers={"Content-Type": "application/json"})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "missing_headers"


def test_a_body_over_the_limit_is_refused_before_verification(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key, settings_overrides={"circle_webhook_max_body_bytes": 32})
    body = json.dumps(_payload()).encode()

    response = _post(client, key, body)

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "body_too_large"


def test_a_signed_but_malformed_body_is_refused(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = b"not json at all"

    response = _post(client, key, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_notification"
    assert "not json at all" not in response.text


def test_a_signed_body_without_a_type_is_refused(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = json.dumps({"notificationId": "n-1"}).encode()

    response = _post(client, key, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_notification"


def test_a_handled_type_without_an_operation_id_is_refused(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: public_key)
    body = json.dumps(_payload(transaction_id=None)).encode()

    response = _post(client, key, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_notification"


def test_an_unfetchable_key_is_a_retryable_refusal(tmp_path):
    key, public_key = _keypair()
    client, _, _, _ = _app(tmp_path, resolver=lambda key_id: (_ for _ in ()).throw(CircleNotificationKeyUnavailable("down")))
    body = json.dumps(_payload()).encode()

    response = _post(client, key, body)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "key_unavailable"


def test_a_store_failure_schedules_nothing(tmp_path, monkeypatch):
    key, public_key = _keypair()
    client, store, _, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    payment = _pending_payment(store, invoice_id)
    monkeypatch.setattr(store, "claim_circle_notification", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("disk")))
    body = json.dumps(_payload()).encode()

    response = _post(client, key, body)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "store_unavailable"
    assert store.get_payment(invoice_id) == payment


def test_the_route_is_disabled_without_the_circle_provider(tmp_path):
    key, public_key = _keypair()
    settings = Settings(_env_file=None, database_path=tmp_path / "mock.sqlite3", payment_provider="mock")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    app = create_app(settings=settings, store=store, webhook_key_resolver=lambda key_id: public_key)
    body = json.dumps(_payload()).encode()

    response = _post(TestClient(app), key, body)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "webhook_not_configured"


def test_a_missing_notification_id_falls_back_to_the_key_and_body_digest(tmp_path):
    key, public_key = _keypair()
    client, store, _, invoice_id = _app(tmp_path, resolver=lambda key_id: public_key)
    _pending_payment(store, invoice_id)
    body = json.dumps(_payload(notification_id=None)).encode()

    assert _post(client, key, body).json() == {"status": "recorded"}
    assert _post(client, key, body).json() == {"status": "duplicate"}
