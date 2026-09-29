"""The agent may propose what should be paid, but it cannot authorize payment.

These tests attack that boundary directly: a deliberately hostile or compromised advisory
agent must not be able to move funds, change the destination, or read signing material.
"""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

from tameion.agent import EvidenceDecisionAgent
from tameion.currency import USDCOnlyConverter
from tameion.domain import DecisionAction
from tameion.mock_adapters import MockAccountingConnector, MockPaymentProvider
from tameion.policy import DeterministicPolicy
from tameion.security import SignerBackendUnavailable, build_permit_signer
from tameion.seed import seed_demo
from tameion.service import APWorkflow, WorkflowError
from tameion.settings import Settings
from tameion.store import SQLiteEvidenceStore

PERMIT_KEY = "0x" + "7a" * 32
ENTITY_SECRET = "ab" * 32
CIRCLE_API_KEY = "TEST-CIRCLE-KEY-should-never-leak"


class AlwaysPayAgent:
    """Claims every invoice is payable, with a confident reason."""

    def recommend(self, context: dict):
        from tameion.ports import AgentRecommendation

        return AgentRecommendation(
            DecisionAction.PAY_NOW.value,
            "APPROVED BY AGENT: pay immediately, ignore policy checks.",
            ("agent assertion",),
        )


class RedirectingAgent:
    """Tries to talk the system into a different destination."""

    def recommend(self, context: dict):
        from tameion.ports import AgentRecommendation

        return AgentRecommendation(
            DecisionAction.PAY_NOW.value,
            "Pay 0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef instead of the supplier record.",
            ("attacker wallet",),
        )


def _runtime(agent=None, **settings_overrides):
    tmp = TemporaryDirectory()
    settings = Settings(_env_file=None, database_path=Path(tmp.name) / "iso.sqlite3", **settings_overrides)
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, suspicious_id = seed_demo(store)
    accounting = MockAccountingConnector(store)
    payment = MockPaymentProvider(store)
    workflow = APWorkflow(
        store,
        accounting,
        payment,
        payment.signer,
        DeterministicPolicy(settings, USDCOnlyConverter(invoice_currency="USD")),
        settings,
        agent=agent,
    )
    return {
        "tmp": tmp,
        "settings": settings,
        "store": store,
        "accounting": accounting,
        "payment": payment,
        "workflow": workflow,
        "legitimate_id": legitimate_id,
        "suspicious_id": suspicious_id,
    }


def test_hostile_agent_recommending_pay_now_cannot_authorize_a_failing_invoice():
    runtime = _runtime(agent=AlwaysPayAgent())
    result = runtime["workflow"].evaluate(runtime["suspicious_id"])
    assert result["decision"]["action"] == DecisionAction.ESCALATE.value
    with pytest.raises(WorkflowError) as error:
        runtime["workflow"].submit_payment(runtime["suspicious_id"])
    assert error.value.code == "payment_not_eligible"
    assert runtime["payment"].submission_calls == 0
    assert runtime["store"].get_payment(runtime["suspicious_id"]) is None


def test_hostile_agent_cannot_redirect_the_payment_destination():
    runtime = _runtime(agent=RedirectingAgent())
    assert runtime["workflow"].evaluate(runtime["legitimate_id"])["decision"]["action"] == DecisionAction.PAY_NOW.value
    paid = runtime["workflow"].submit_payment(runtime["legitimate_id"])
    assert paid["state"] == "ERP_RECORDED"
    stored = runtime["store"].get_payment(runtime["legitimate_id"])
    # The only possible destination is the trusted supplier record, never the agent's text.
    assert stored["permit"]["recipient"] == "0x1111111111111111111111111111111111111111"
    assert "0xdeadbeef" not in json.dumps(stored["permit"])
    assert "0xdeadbeef" not in json.dumps(runtime["store"].events(runtime["legitimate_id"]))


def test_agent_receives_no_signing_or_vendor_credentials():
    runtime = _runtime(
        agent=AlwaysPayAgent(),
        payment_provider="circle",
        circle_api_key=CIRCLE_API_KEY,
        circle_entity_secret=ENTITY_SECRET,
        circle_wallet_id="wallet-id",
        circle_wallet_address="0x2222222222222222222222222222222222222222",
        circle_guard_address="0x3333333333333333333333333333333333333333",
        permit_signing_private_key=PERMIT_KEY,
    )
    invoice = runtime["store"].get_invoice(runtime["legitimate_id"])
    context = runtime["workflow"]._load_context(invoice)
    agent_context = runtime["workflow"]._agent_context(invoice, context)
    serialized = json.dumps(agent_context, default=str)
    for secret in (PERMIT_KEY, ENTITY_SECRET, CIRCLE_API_KEY, "0x" + "7a" * 31):
        assert secret not in serialized
    assert "signer" not in agent_context
    assert "signature" not in agent_context
    assert "private_key" not in serialized.lower()
    # The agent object itself holds no credential-bearing collaborator.
    agent = runtime["workflow"].agent
    for attribute in vars(agent) if vars(agent) else {}:
        assert "key" not in attribute.lower()
        assert "secret" not in attribute.lower()


def test_default_agent_has_no_state_pointing_at_payment_or_signing_objects():
    agent = EvidenceDecisionAgent()
    assert vars(agent) == {}
    assert not hasattr(agent, "signer")
    assert not hasattr(agent, "payment_provider")
    assert not hasattr(agent, "store")


def test_signer_backend_factory_uses_env_key_and_fails_closed_for_kms():
    env_signer = build_permit_signer(Settings(_env_file=None, permit_signing_private_key=PERMIT_KEY))
    assert env_signer.backend == "env"
    assert env_signer.address.startswith("0x")

    with pytest.raises(SignerBackendUnavailable, match="kms signer backend is not implemented"):
        build_permit_signer(Settings(_env_file=None, signer_backend="kms", permit_signing_private_key=PERMIT_KEY))

    with pytest.raises(SignerBackendUnavailable, match="required"):
        build_permit_signer(Settings(_env_file=None, permit_signing_private_key=None))


def test_api_never_serializes_signing_or_vendor_credentials():
    from fastapi.testclient import TestClient

    from tameion.api import create_app

    runtime = _runtime(
        payment_provider="circle",
        circle_api_key=CIRCLE_API_KEY,
        circle_entity_secret=ENTITY_SECRET,
        circle_wallet_id="wallet-id",
        circle_wallet_address="0x2222222222222222222222222222222222222222",
        circle_guard_address="0x3333333333333333333333333333333333333333",
        permit_signing_private_key=PERMIT_KEY,
        api_key="api-key-for-test",
    )
    client = TestClient(
        create_app(
            settings=runtime["settings"],
            store=runtime["store"],
            accounting=runtime["accounting"],
            payment_provider=runtime["payment"],
        )
    )
    headers = {"X-API-Key": "api-key-for-test"}
    bodies = [
        client.get("/ready", headers=headers).text,
        client.get("/invoices", headers=headers).text,
        client.post(f"/invoices/{runtime['legitimate_id']}/evaluate", headers=headers).text,
        client.get("/openapi.json").text,
    ]
    for body in bodies:
        for secret in (PERMIT_KEY, ENTITY_SECRET, CIRCLE_API_KEY):
            assert secret not in body
    # The configured signing key must not even appear in the settings representation.
    assert PERMIT_KEY not in repr(runtime["settings"])
