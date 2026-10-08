"""The guided console is a real workflow, never a shortcut around payment safety."""
from pathlib import Path
import json
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from arc_payables.api import create_app
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore


def client_for(tmp_path, **overrides):
    settings = Settings(_env_file=None, database_path=tmp_path / "journey.sqlite3", **overrides)
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    return TestClient(create_app(settings=settings, store=store)), store


def test_empty_demo_can_be_started_without_credentials_and_never_pays(tmp_path):
    client, store = client_for(tmp_path)
    context = client.get("/setup").json()["deployment"]
    assert context == {
        "mode": "demo", "payment_provider": "mock", "accounting_provider": "mock",
        "screening_provider": "fixture", "demo_available": True,
    }
    assert client.post("/demo/start").json()["seeded"] is True
    assert len(store.list_invoices()) == 2
    assert all(row["state"] == "RECEIVED" for row in client.get("/invoices").json())
    assert client.get("/payments").json()["payments"] == []


def test_the_demo_walks_evidence_payment_ledger_and_real_verification(tmp_path):
    client, store = client_for(tmp_path)
    assert client.post("/demo/start").status_code == 200
    clean = "/invoices/demo-invoice-legitimate"
    blocked = "/invoices/demo-invoice-suspicious"
    decision = client.post(f"{clean}/evaluate").json()
    assert decision["decision"]["action"] == "PAY_NOW"
    suspicious = client.post(f"{blocked}/evaluate").json()
    assert any(not check["passed"] and not check["overridable"] for check in suspicious["decision"]["policy_checks"])
    assert client.post(f"{blocked}/payment").status_code == 409
    assert store.get_payment("demo-invoice-suspicious") is None
    paid = client.post(f"{clean}/payment")
    assert paid.status_code == 200, paid.text
    assert paid.json()["state"] == "ERP_RECORDED"
    report = client.get("/payments/demo-invoice-legitimate").json()
    assert report["settlement"]["recipient"] == "0x1111111111111111111111111111111111111111"
    audit = client.get("/audit/verify").json()
    assert audit["ok"] and audit["checked"] > 0 and audit["signed"] == audit["checked"]
    before = client.get(clean).json()
    events = client.get(f"{clean}/events").json()
    assert client.post("/demo/start").json()["seeded"] is False
    assert client.get(clean).json() == before
    assert client.get(f"{clean}/events").json() == events
    assert client.get("/payments/demo-invoice-legitimate").status_code == 200


def test_demo_initialization_respects_configured_api_auth(tmp_path):
    client, store = client_for(tmp_path, api_key="private-demo")
    assert client.post("/demo/start").status_code == 401
    assert store.list_invoices() == []
    assert client.post("/demo/start", headers={"X-API-Key": "private-demo"}).status_code == 200


@pytest.mark.parametrize("overrides", [
    {"payment_provider": "circle"}, {"payment_provider": "local"},
    {"accounting_provider": "frappe"},
    {"screening_provider": "opensanctions"}, {"decision_layer": "dual_process"},
])
def test_demo_initialization_refuses_external_or_mixed_configuration(tmp_path, overrides):
    client, store = client_for(tmp_path, api_key="test-access", auth_mode="testnet_tokens", **overrides)
    headers = {"X-API-Key": "test-access"}
    context = client.get("/setup", headers=headers).json()["deployment"]
    assert not context["demo_available"]
    response = client.post("/demo/start", headers=headers)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "demo_unavailable"
    assert store.list_invoices() == []


def test_demo_never_rewrites_existing_non_demo_records(tmp_path):
    client, store = client_for(tmp_path)
    store.seed_fixture("supplier", "SUP-ACME-001", {"name": "Do not overwrite"})
    response = client.post("/demo/start")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "demo_records_exist"
    assert store.get_fixture("supplier", "SUP-ACME-001")["name"] == "Do not overwrite"
    assert store.list_invoices() == []


def test_demo_refuses_an_injected_non_mock_provider_even_if_settings_say_mock(tmp_path):
    settings = Settings(_env_file=None, database_path=tmp_path / "injected.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    client = TestClient(create_app(settings=settings, store=store, payment_provider=object()))
    response = client.post("/demo/start")
    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "api_auth_not_configured"
    assert store.list_invoices() == []


@pytest.mark.skipif(shutil.which("node") is None, reason="node is required for console behavior checks")
def test_console_safety_helpers_and_failure_boundaries(tmp_path):
    web = Path(__file__).resolve().parents[1] / "src" / "arc_payables" / "web"
    for module in web.glob("*.js"):
        shutil.copy(module, tmp_path / module.name)
    (tmp_path / "package.json").write_text(json.dumps({"type": "module"}))
    runner = tmp_path / "check.mjs"
    runner.write_text(r'''
import assert from 'node:assert/strict';
globalThis.document = { addEventListener() {} };
globalThis.window = { addEventListener() {} };
globalThis.sessionStorage = { getItem() { return ''; } };
globalThis.fetch = async () => ({ok: true, json: async () => ({})});
const app = await import('./app.js');
const { invoiceActions } = await import('./invoice.js');
const { journeyProgress } = await import('./journey.js');
assert.equal(app.workerHealth(null).tone, 'warn');
assert.match(app.workerHealth({}).label, /No worker pass/);
const now = Date.parse('2026-10-03T10:00:00Z');
assert.equal(app.workerHealth({last: {outcome: 'ok', finished_at: '2026-10-03T09:55:01Z'}}, now).tone, 'ok');
assert.equal(app.workerHealth({last: {outcome: 'ok', finished_at: '2026-10-03T09:55:00Z'}}, now).tone, 'warn');
assert.equal(app.workerHealth({last: {outcome: 'failed', finished_at: '2026-10-03T10:00:00Z'}}, now).tone, 'bad');
assert.equal(app.workerHealth({last: {outcome: 'ok', finished_at: 'not a time'}}, now).tone, 'warn');
const reviewable = {code: 'amount_limit', passed: false, overridable: true, requires_human: true};
const blocking = {code: 'accounting_invoice_match', passed: false, overridable: false, requires_human: true};
const escalated = {state: 'ESCALATED', decision: {action: 'ESCALATE', policy_checks: [reviewable]}};
assert.equal(invoiceActions(escalated).approve, true);
assert.equal(invoiceActions({...escalated, decision: {...escalated.decision, policy_checks: [reviewable, blocking]}}).approve, false);
assert.equal(invoiceActions({decision: {action: 'PAY_NOW', policy_checks: [blocking]}}).pay, false);
assert.equal(invoiceActions({decision: {action: 'PAY_NOW', policy_checks: [{passed: true}]}}).pay, true);
const paid = {state: 'ERP_PENDING', payment: {confirmation_status: 'CONFIRMED'}, decision: {action: 'PAY_NOW', policy_checks: []}};
assert.equal(invoiceActions(paid).pay, false);
assert.equal(invoiceActions(paid).evaluate, false);
assert.equal(invoiceActions(paid).writeback, true);
assert.equal(invoiceActions({...paid, state: 'ERP_RECORDED'}).writeback, false);
assert.equal(invoiceActions({...paid, state: 'NEEDS_RECONCILIATION', payment: {confirmation_status: 'UNKNOWN'}}).reconcile, true);
assert.equal(journeyProgress([]).captured, false);
assert.equal(journeyProgress([{invoice: {id: 'demo-invoice-legitimate'}, state: 'ERP_PENDING'}]).recorded, false);
assert.equal(journeyProgress([{invoice: {id: 'demo-invoice-legitimate'}, state: 'ERP_RECORDED'}]).recorded, true);
assert.match(app.deploymentLabel(null), /unknown/);
// The client must surface both FastAPI and workflow error envelopes.
globalThis.fetch = async () => ({ok: false, status: 409, text: async () => JSON.stringify({error: {code: 'evidence_changed_after_evaluation', message: 'Re-evaluate first.'}})});
await assert.rejects(app.api('/test'), /evidence_changed_after_evaluation: Re-evaluate first/);
globalThis.fetch = async () => ({ok: false, status: 401, text: async () => JSON.stringify({detail: {code: 'unauthorized', message: 'Key required.'}})});
await assert.rejects(app.api('/test'), /unauthorized: Key required/);
globalThis.fetch = async () => ({ok: true, text: async () => JSON.stringify({})});
await assert.rejects(app.deploymentContext(), /environment_unknown/);
''')
    result = subprocess.run([shutil.which("node"), str(runner)], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
