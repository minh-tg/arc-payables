"""The two screens an operator reads first: what is wrong, and what is not configured yet.

Both are read-only, so the tests are about what they must never say and what they must always
say. A setup screen that echoes a credential is worse than no setup screen, and an attention list
that hides an unconfirmed settlement is worse than no list.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from arc_payables.attention import build_attention
from arc_payables.domain import WorkflowState, utcnow
from arc_payables.settings import Settings
from arc_payables.setup_check import inventory, live_checks

SECRET_KEY = "circle-key-that-must-never-be-echoed"
SECRET_ENTITY = "ab" * 32
TOKENISED_RPC = "https://rpc.example.test/v1/swrm_deadbeefdeadbeefdeadbeef"


def _client(runtime, **overrides) -> TestClient:
    from arc_payables.api import create_app

    settings = Settings(
        _env_file=None,
        database_path=runtime["settings"].database_path,
        api_key="setup-test-key",
        **overrides,
    )
    app = create_app(
        settings=settings,
        store=runtime["store"],
        accounting=runtime["accounting"],
        payment_provider=runtime["payment"],
    )
    return TestClient(app)


# --------------------------------------------------------------------------------------
# Setup: it may never echo a credential
# --------------------------------------------------------------------------------------


def test_a_live_deployment_reports_every_missing_setting_with_its_consequence(runtime):
    body = _client(runtime, payment_provider="circle", accounting_provider="frappe").get(
        "/setup", headers={"X-API-Key": "setup-test-key"}
    ).json()

    assert body["ready"] is False
    assert "CIRCLE_ENTITY_SECRET" in body["missing"]
    assert "FRAPPE_COMPANY" in body["missing"]
    group = next(item for item in body["groups"] if item["name"].startswith("Settlement"))
    assert group["complete"] is False
    row = next(item for item in group["requirements"] if item["env"] == "CIRCLE_GUARD_ADDRESS")
    assert row["state"] == "missing"
    # A missing setting has to say what stops working, or the operator is only told that something is.
    assert "budget to enforce" in row["breaks"]


def test_the_setup_report_never_returns_a_secret_value():
    settings = Settings(
        _env_file=None,
        payment_provider="circle",
        circle_api_key=SECRET_KEY,
        circle_entity_secret=SECRET_ENTITY,
        circle_wallet_id="wallet-id",
        circle_wallet_address="0x784fcf77d0f718210dbadbce1a464bcb67e58aed",
        circle_guard_address="0xbe0477081f90e68b6699a585d5d31ad93d96f318",
        circle_rpc_url=TOKENISED_RPC,
        permit_signing_private_key="0x" + "11" * 32,
    )
    body = inventory(settings)
    rendered = str(body)

    assert SECRET_KEY not in rendered
    assert SECRET_ENTITY not in rendered
    assert "0x" + "11" * 32 not in rendered
    # An RPC endpoint handed out by Arc's tooling carries a token in the path.
    assert "swrm_deadbeef" not in rendered
    assert "https://rpc.example.test/" in rendered

    rows = {row["env"]: row for group in body["groups"] for row in group["requirements"]}
    assert rows["CIRCLE_API_KEY"]["state"] == "set"
    assert rows["CIRCLE_API_KEY"]["value"] is None
    assert rows["CIRCLE_ENTITY_SECRET"]["value"] is None
    assert rows["CIRCLE_RPC_URL"]["value"] == "https://rpc.example.test/"


def test_the_local_demo_is_complete_without_a_key_and_says_so_without_alarming(runtime):
    """An empty API key is the demo working as designed, not a defect to shout about."""
    body = inventory(runtime["settings"])

    assert body["ready"] is True
    assert body["missing"] == []
    rows = {row["env"]: row for group in body["groups"] for row in group["requirements"]}
    assert rows["API_KEY"]["state"] == "not_needed"
    # The demo limits are visible as examples rather than treated as mistakes.
    assert "MAX_INVOICE_USDC" in body["demo_defaults"]
    assert rows["MAX_INVOICE_USDC"]["state"] == "demo_default"


def test_setup_endpoints_need_the_key_and_are_documented(runtime):
    client = _client(runtime)
    assert client.get("/setup").status_code == 401
    assert client.post("/setup/checks").status_code == 401
    spec = client.get("/openapi.json").json()
    assert "/setup" in spec["paths"] and "/setup/checks" in spec["paths"] and "/attention" in spec["paths"]


def test_the_console_can_read_every_definition_it_names(runtime):
    """The guided view renders only what this payload publishes.

    Dropping a table from here removes a whole feature from the browser and fails nothing else, so
    the payload is pinned to the vocabulary the console asks for. It is fetched without a key, which
    is how the console actually reaches it.
    """
    response = _client(runtime).get("/explanations.json")
    assert response.status_code == 200
    body = response.json()
    for name in (
        "states", "decisions", "checks", "screening", "attention", "outcomes",
        "confirmations", "steps", "passes", "alerts", "writeback", "guard", "setup", "tiers", "concepts",
    ):
        assert body.get(name), f"{name} is missing from the published explanations"
        for code, row in body[name].items():
            assert row.get("plain"), f"{name}.{code} was published with no words"
    # A beginner reads this one first, so its absence is a broken guided view rather than a gap.
    assert "digital dollar" in body["concepts"]["usdc"]["plain"]


def test_the_live_checks_report_a_mock_deployment_as_nothing_to_reach(runtime):
    outcome = live_checks(runtime["workflow"], runtime["settings"])

    names = [check["name"] for check in outcome["checks"]]
    assert names == ["payment provider", "arc testnet and guard", "accounting"]
    provider = outcome["checks"][0]
    assert provider["ok"] is True
    assert "nothing to reach" in provider["detail"]


def test_the_chain_check_reports_what_it_read_back_off_the_chain(runtime):
    """The check has to survive having no client injected and report the chain's own answers.

    The first version of this test asserted the check's name and not its result, which is how a
    check that raised on every call passed a green suite.
    """
    from test_verify_arc import _rpc, _settings

    settings = _settings(
        circle_wallet_address="0x784fcf77d0f718210dbadbce1a464bcb67e58aed",
        circle_guard_address="0xbe0477081f90e68b6699a585d5d31ad93d96f318",
    )
    outcome = live_checks(runtime["workflow"], settings, rpc=_rpc())

    chain = next(check for check in outcome["checks"] if check["name"] == "arc testnet and guard")
    assert chain["ok"] is True
    assert any("chain id is Arc Testnet" in line for line in chain["findings"])
    assert any("budgets" in line for line in chain["findings"])


def test_the_chain_check_fails_closed_on_another_chain(runtime):
    from test_verify_arc import _rpc, _settings

    settings = _settings(
        circle_wallet_address="0x784fcf77d0f718210dbadbce1a464bcb67e58aed",
        circle_guard_address="0xbe0477081f90e68b6699a585d5d31ad93d96f318",
    )
    outcome = live_checks(runtime["workflow"], settings, rpc=_rpc(chain_id=1))

    chain = next(check for check in outcome["checks"] if check["name"] == "arc testnet and guard")
    assert chain["ok"] is False
    assert outcome["ok"] is False


def test_a_broken_accounting_probe_is_reported_not_raised(runtime, monkeypatch):
    settings = Settings(
        _env_file=None, database_path=runtime["settings"].database_path, accounting_provider="frappe",
        frappe_url="http://127.0.0.1:8080", frappe_api_key="k", frappe_api_secret="s",
    )

    def broken(*args, **kwargs):
        raise RuntimeError("the ledger refused the credentials")

    # The mock connector has no read to probe at all, so the probe is attached to the instance.
    monkeypatch.setattr(runtime["accounting"], "list_documents", broken, raising=False)
    outcome = live_checks(runtime["workflow"], settings)

    accounting = next(check for check in outcome["checks"] if check["name"] == "accounting")
    assert accounting["ok"] is False
    assert "refused the credentials" in accounting["detail"]
    assert outcome["ok"] is False


# --------------------------------------------------------------------------------------
# Attention: it may never hide money
# --------------------------------------------------------------------------------------


def _payment(runtime, invoice_id: str, updates: dict, state: str) -> None:
    runtime["store"].update_payment(invoice_id, updates, state, "TEST_SETUP")


def test_an_escalated_invoice_is_listed_with_its_amount(runtime):
    runtime["workflow"].evaluate(runtime["suspicious_id"])

    body = build_attention(runtime["workflow"])

    item = next(entry for entry in body["items"] if entry["code"] == "invoice_escalated")
    assert item["count"] == 1
    assert item["detail"]["invoices"][0]["invoice_id"] == runtime["suspicious_id"]
    assert item["severity"] == "warning"


def test_money_that_moved_is_never_hidden(runtime):
    """A settlement nobody confirmed, and a confirmed one the ledger has not taken."""
    runtime["workflow"].evaluate(runtime["legitimate_id"])
    runtime["workflow"].submit_payment(runtime["legitimate_id"])
    _payment(runtime, runtime["legitimate_id"], {"erp_status": "PENDING"}, WorkflowState.CONFIRMED.value)

    body = build_attention(runtime["workflow"])

    unrecorded = next(entry for entry in body["items"] if entry["code"] == "payments_not_in_ledger")
    assert unrecorded["severity"] == "critical"
    assert unrecorded["detail"]["payments"][0]["transaction_hash"]

    _payment(runtime, runtime["legitimate_id"], {"confirmation_status": "UNCERTAIN"}, WorkflowState.NEEDS_RECONCILIATION.value)
    body = build_attention(runtime["workflow"])
    assert any(entry["code"] == "settlements_unconfirmed" for entry in body["items"])
    assert any(entry["code"] == "invoice_needs_reconciliation" for entry in body["items"])
    assert body["ok"] is False


def test_a_breached_reserve_and_a_broken_chain_are_critical(runtime, monkeypatch):
    runtime["workflow"].settings.min_reserve_usdc = Decimal("6000")

    body = build_attention(runtime["workflow"])

    reserve = next(entry for entry in body["items"] if entry["code"] == "reserve_breached")
    assert reserve["severity"] == "critical"
    # Critical first, so the top of the page is the worst thing.
    assert body["items"][0]["severity"] == "critical"


def test_attention_reads_the_same_snapshot_as_the_metrics(runtime):
    """The two must not disagree about what is wrong."""
    from arc_payables.metrics import collect

    snapshot = collect(runtime["workflow"])
    body = build_attention(runtime["workflow"])

    assert body["audit_entries"] == snapshot["audit_entries"]
    assert body["treasury_usdc"] == snapshot["treasury_usdc"]


def test_attention_reports_the_alerts_the_worker_recorded(runtime):
    now = utcnow().isoformat()
    stored = [{"code": "reserve_breached", "severity": "critical", "summary": "below the floor", "detail": {}}]
    runtime["store"].record_worker_run(now, now, "degraded", {"outcome": "degraded", "alerts": stored})

    body = build_attention(runtime["workflow"])

    assert body["alerts"] == stored


def test_attention_requires_the_key(runtime):
    client = _client(runtime)
    assert client.get("/attention").status_code == 401
    assert client.get("/attention", headers={"X-API-Key": "setup-test-key"}).status_code == 200
