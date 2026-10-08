"""Discriminating checks at the identity-to-financial-authorization boundary."""
import hashlib
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from arc_payables.api import create_app
from arc_payables.auth import LOGIN_COOKIE, SESSION_COOKIE
from arc_payables.seed import seed_demo
from arc_payables.settings import Settings
from oidc_fixture import FakeOIDC


@pytest.fixture
def desk(tmp_path):
    provider = FakeOIDC()
    settings = Settings(
        _env_file=None, database_path=tmp_path / "identity.sqlite3", auth_mode="oidc",
        payment_provider="mock", accounting_provider="mock", screening_provider="fixture",
        decision_layer="heuristics", oidc_issuer=provider.issuer, oidc_client_id=provider.client_id,
        oidc_redirect_uri="https://console.example/auth/callback",
        oidc_subject_roles={role: (role,) for role in ("reader", "operator", "approver", "payer", "admin")},
        api_key="legacy-key", approval_token="legacy-approval",
    )
    app = create_app(settings=settings, oidc_client=provider.client())
    return app, settings, provider


def browser(desk):
    return TestClient(desk[0], base_url="https://console.example")


def sign_in(desk, subject, client=None):
    client = client or browser(desk)
    start = client.get("/auth/login", follow_redirects=False)
    assert start.status_code == 302
    code, state = desk[2].code_for(start.headers["location"], subject)
    response = client.get("/auth/callback?" + urlencode({"code": code, "state": state}), follow_redirects=False)
    return client, response


def signed(desk, subject):
    client, response = sign_in(desk, subject)
    assert response.status_code == 303, response.text
    session = client.get("/auth/session")
    assert session.status_code == 200
    client.headers.update({"Origin": "https://console.example", "X-CSRF-Token": session.json()["csrf_token"]})
    return client


def test_session_cookie_security_pkce_identity_and_no_token_storage(desk):
    client, response = sign_in(desk, "approver")
    assert response.status_code == 303
    assert response.headers["location"] == "/console/"
    cookie = next(v for v in response.headers.get_list("set-cookie") if v.startswith(SESSION_COOKIE + "="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=lax" in cookie
    assert "no-store" in response.headers["cache-control"]
    session = client.get("/auth/session").json()
    assert session["identity"]["subject"] == "approver"
    assert session["permissions"] == ["approve", "read"]
    raw = client.cookies.get(SESSION_COOKIE)
    with desk[0].state.store._connect() as connection:
        row = dict(connection.execute("SELECT * FROM auth_sessions").fetchone())
        assert row["session_hash"] == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in json.dumps(row)
        assert "test-access-token" not in json.dumps(row)
        assert connection.execute("SELECT COUNT(*) FROM auth_login_states").fetchone()[0] == 0
    token_request = next(r for r in desk[2].requests if r.url.path == "/token")
    assert b"code_verifier=" in token_request.content


@pytest.mark.parametrize("overrides", [
    {"iss": "https://other-business.example"}, {"aud": "other-application"},
    {"nonce": "copied-from-another-browser"}, {"nonce": None},
    {"exp": 1}, {"iat": int(time.time()) + 120}, {"iat": True},
    {"auth_time": int(time.time()) - 901}, {"auth_time": int(time.time()) + 120},
    {"amr": ["pwd"]}, {"amr": "mfa"}, {"amr": None},
    {"aud": ["tameion-test", "other"]}, {"azp": "other"},
    {"at_hash": "not-the-access-token"}, {"sub": ""}, {"sub": 123},
])
def test_invalid_or_non_mfa_identity_never_gets_a_session(desk, overrides):
    desk[2].claim_overrides = overrides
    client, response = sign_in(desk, "approver")
    assert response.status_code == 401, response.text
    assert client.get("/invoices").status_code == 401
    assert client.cookies.get(SESSION_COOKIE) is None
    assert desk[0].state.workflow.payment_provider.submission_calls == 0


@pytest.mark.parametrize("claim", ["iss", "aud", "sub", "exp", "iat", "nonce", "auth_time"])
def test_missing_required_identity_claims_fail_closed(desk, claim):
    desk[2].claims_to_drop = {claim}
    assert sign_in(desk, "reader")[1].status_code == 401


def test_forged_rsa_signature_is_rejected(desk):
    from cryptography.hazmat.primitives.asymmetric import rsa
    desk[2].signing_key_override = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert sign_in(desk, "approver")[1].status_code == 401


def test_algorithm_confusion_is_rejected(desk):
    desk[2].algorithm = "HS256"
    _, response = sign_in(desk, "approver")
    assert response.status_code == 401


def test_unknown_subject_and_claimed_admin_role_do_not_grant_access(desk):
    desk[2].claim_overrides = {"roles": ["admin", "approver", "payer"]}
    _, response = sign_in(desk, "unknown-subject")
    assert response.status_code == 403
    reader = signed(desk, "reader")
    assert reader.get("/auth/session").json()["permissions"] == ["read"]
    assert reader.post("/demo/start").status_code == 403


def test_state_is_browser_bound_and_single_use(desk):
    client = browser(desk)
    start = client.get("/auth/login", follow_redirects=False)
    cookie = client.cookies.get(LOGIN_COOKIE)
    code, state = desk[2].code_for(start.headers["location"], "operator")
    path = "/auth/callback?" + urlencode({"code": code, "state": state})
    assert browser(desk).get(path, follow_redirects=False).status_code == 401
    attacker = browser(desk)
    attacker.cookies.set(LOGIN_COOKIE, state, domain="console.example", path="/auth")
    assert attacker.get(path, follow_redirects=False).status_code == 401
    assert client.get(path, follow_redirects=False).status_code == 303
    client.cookies.set(LOGIN_COOKIE, cookie, domain="console.example", path="/auth")
    assert client.get(path, follow_redirects=False).status_code == 401


def test_corrupted_pkce_verifier_blocks_token_exchange(desk):
    client = browser(desk)
    start = client.get("/auth/login", follow_redirects=False)
    code, state = desk[2].code_for(start.headers["location"], "operator")
    with desk[0].state.store._connect() as connection:
        connection.execute("UPDATE auth_login_states SET verifier='wrong-verifier'")
    response = client.get("/auth/callback?" + urlencode({"code": code, "state": state}), follow_redirects=False)
    assert response.status_code == 503
    assert client.get("/invoices").status_code == 401


def test_expired_state_and_removed_role_mapping_block_access(desk):
    client = browser(desk)
    start = client.get("/auth/login", follow_redirects=False)
    code, state = desk[2].code_for(start.headers["location"], "reader")
    with desk[0].state.store._connect() as connection:
        connection.execute("UPDATE auth_login_states SET expires_at=?", (int(time.time()),))
    assert client.get("/auth/callback?" + urlencode({"code": code, "state": state}), follow_redirects=False).status_code == 401
    client = signed(desk, "reader")
    del desk[1].oidc_subject_roles["reader"]
    assert client.get("/invoices").status_code == 403


def test_subject_revocation_wins_session_creation(desk, monkeypatch):
    client = browser(desk)
    start = client.get("/auth/login", follow_redirects=False)
    code, state = desk[2].code_for(start.headers["location"], "approver")
    store = desk[0].state.store
    original = store.auth_create_session
    def revoke_then_create(*args):
        store.auth_revoke_subject(desk[1].oidc_issuer, "approver", "admin", int(time.time()))
        return original(*args)
    monkeypatch.setattr(store, "auth_create_session", revoke_then_create)
    assert client.get("/auth/callback?" + urlencode({"code": code, "state": state}), follow_redirects=False).status_code == 403


def test_subject_revocation_wins_approval_recording(desk, monkeypatch):
    invoice_id, body = reviewed_invoice(desk)
    checker = signed(desk, "approver")
    store = desk[0].state.store
    original = store.record_approval
    def revoke_then_record(*args):
        store.auth_revoke_subject(desk[1].oidc_issuer, "approver", "admin", int(time.time()))
        return original(*args)
    monkeypatch.setattr(store, "record_approval", revoke_then_record)
    response = checker.post(f"/invoices/{invoice_id}/approval", json=body)
    assert response.status_code == 403
    assert not store.get_approval(invoice_id)


def test_legacy_approval_is_not_promoted_but_new_review_preserves_history(desk):
    invoice_id, body = reviewed_invoice(desk)
    store = desk[0].state.store
    decision = store.get_decision(invoice_id)
    store.record_approval(invoice_id, {**body, "reviewer": "unverified legacy reviewer",
                                      "evidence_hash": decision["evidence_hash"],
                                      "created_at": datetime.now(timezone.utc).isoformat()})
    assert signed(desk, "operator").post(f"/invoices/{invoice_id}/evaluate").json()["state"] == "ESCALATED"
    assert signed(desk, "approver").post(f"/invoices/{invoice_id}/approval", json=body).status_code == 200
    response = signed(desk, "payer").post(f"/invoices/{invoice_id}/payment")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "ERP_RECORDED"
    assert len(store.get_approval(invoice_id)) == 2


@pytest.mark.parametrize("invalidate", ["logout", "expiry"])
def test_session_ending_during_signing_blocks_new_authorization(desk, monkeypatch, invalidate):
    invoice_id, body = reviewed_invoice(desk)
    signed(desk, "approver").post(f"/invoices/{invoice_id}/approval", json=body)
    payer = signed(desk, "payer")
    workflow = desk[0].state.workflow
    original = workflow.signer.sign
    submitted = []
    monkeypatch.setattr(workflow.payment_provider, "submit_authorized", lambda *args, **kwargs: submitted.append(args))
    def invalidate_then_sign(permit):
        result = original(permit)
        if invalidate == "logout":
            workflow.store.auth_delete_session(hashlib.sha256(payer.cookies.get(SESSION_COOKIE).encode()).hexdigest())
        else:
            with workflow.store._connect() as connection:
                connection.execute("UPDATE auth_sessions SET expires_at=?", (int(time.time()),))
        return result
    monkeypatch.setattr(workflow.signer, "sign", invalidate_then_sign)
    response = payer.post(f"/invoices/{invoice_id}/payment")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_authorization_conflict"
    assert not workflow.store.get_payment(invoice_id)
    assert submitted == []


def test_confidential_client_basic_auth_uses_oauth_form_encoding(desk):
    import base64
    desk[1].oidc_client_id = desk[2].client_id = "client:with space"
    desk[1].oidc_client_secret = "test:secret+with space"
    captured = []
    desk[0].state.identity_auth.client.event_hooks["request"].append(
        lambda request: captured.append(request.headers.get("Authorization")))
    signed(desk, "reader")
    header = next(value for value in captured if value is not None)
    assert base64.b64decode(header.split(" ", 1)[1]).decode() == "client%3Awith+space:test%3Asecret%2Bwith+space"


def test_oidc_client_secret_is_not_exposed_in_configuration_or_errors(desk):
    desk[1].oidc_client_secret = "OIDC-SECRET-MUST-NOT-LEAK"
    client = signed(desk, "reader")
    for path in ("/auth/config", "/auth/session", "/setup"):
        response = client.get(path)
        assert "OIDC-SECRET-MUST-NOT-LEAK" not in response.text
        if path == "/setup":
            access = next(group for group in response.json()["groups"] if group["name"] == "Access")
            assert access["complete"]
            assert not any(row["env"] in {"API_KEY", "APPROVAL_TOKEN"} for row in access["requirements"])
        assert "no-store" in response.headers["cache-control"]


def test_oidc_scrape_token_cannot_be_used_as_staff_credentials(desk):
    desk[1].metrics_api_key = "metrics-only-key"
    client = browser(desk)
    headers = {"Authorization": "Bearer metrics-only-key"}
    assert client.get("/metrics", headers=headers).status_code == 200
    assert client.get("/invoices", headers=headers).status_code == 401
    assert client.post("/demo/start", headers=headers).status_code == 401


def test_upstream_outage_and_untrusted_discovery_endpoint_fail_closed(desk):
    desk[2].unavailable = True
    assert browser(desk).get("/auth/login", follow_redirects=False).status_code == 503
    desk[2].unavailable = False
    desk[2].metadata_overrides = {"token_endpoint": "https://attacker.example/token"}
    assert browser(desk).get("/auth/login", follow_redirects=False).status_code == 503
    assert all(r.url.host == "identity.example" for r in desk[2].requests)


def test_key_rotation_refreshes_bounded_cache(desk):
    assert sign_in(desk, "reader")[1].status_code == 303
    desk[2].kid = "rotated-key"
    desk[0].state.identity_auth._fetched_at = time.monotonic() - 16
    assert sign_in(desk, "operator")[1].status_code == 303


@pytest.mark.parametrize("role", ["reader", "operator", "approver", "payer", "admin"])
def test_role_separation_blocks_other_financial_actions(desk, role):
    client = signed(desk, role)
    invoice_id, _ = seed_demo(desk[0].state.store)
    assert client.get("/invoices").status_code == 200
    if role != "operator":
        assert client.post(f"/invoices/{invoice_id}/evaluate").status_code == 403
        assert client.post(f"/invoices/{invoice_id}/link", json={"purchase_invoice_id": "untrusted"}).status_code == 403
    if role != "approver":
        assert client.post(f"/invoices/{invoice_id}/approval", json={"approved": True, "note": "test"}).status_code == 403
    if role != "payer":
        assert client.post(f"/invoices/{invoice_id}/payment").status_code == 403
        assert client.post(f"/invoices/{invoice_id}/payment/reconcile").status_code == 403
    if role != "admin":
        assert client.post("/auth/revoke", json={"subject": "reader"}).status_code == 403
    assert desk[0].state.workflow.payment_provider.submission_calls == 0


def test_shared_tokens_and_forged_cookies_cannot_bypass_oidc(desk):
    client = browser(desk)
    headers = {"X-API-Key": "legacy-key", "X-Approval-Token": "legacy-approval"}
    assert client.get("/invoices", headers=headers).status_code == 401
    client.cookies.set(SESSION_COOKIE, "forged")
    assert client.get("/invoices", headers=headers).status_code == 401


def test_cookie_writes_need_csrf_and_the_configured_origin(desk):
    client = signed(desk, "operator")
    del client.headers["X-CSRF-Token"]
    assert client.post("/demo/start").status_code == 403
    client.headers["X-CSRF-Token"] = client.get("/auth/session").json()["csrf_token"]
    client.headers["Origin"] = "https://attacker.example"
    assert client.post("/demo/start").status_code == 403
    client.headers["Origin"] = "https://console.example"
    assert client.post("/demo/start").status_code == 200


def reviewed_invoice(desk):
    workflow = desk[0].state.workflow
    invoice_id, _ = seed_demo(workflow.store)
    desk[1].max_invoice_usdc = Decimal("100")
    operator = signed(desk, "operator")
    response = operator.post(f"/invoices/{invoice_id}/evaluate")
    assert response.status_code == 200
    assert response.json()["state"] == "ESCALATED"
    checks = response.json()["decision"]["policy_checks"]
    scopes = [c["code"] for c in checks if not c["passed"] and c["requires_human"] and c["overridable"]]
    assert scopes
    return invoice_id, {"approved": True, "note": "Approved independent evidence", "acknowledged_checks": scopes}


def test_approval_is_server_attributed_and_bound_in_signed_audit(desk):
    invoice_id, body = reviewed_invoice(desk)
    checker = signed(desk, "approver")
    assert checker.post(f"/invoices/{invoice_id}/approval", json={**body, "reviewer": "someone else"}).status_code == 422
    assert checker.post(f"/invoices/{invoice_id}/approval", json={**body, "reviewer_identity": {"subject": "admin"}}).status_code == 422
    response = checker.post(f"/invoices/{invoice_id}/approval", json=body)
    assert response.status_code == 200, response.text
    record = desk[0].state.store.get_approval(invoice_id)[-1]
    actor = checker.get("/auth/session").json()["identity"]
    assert record["reviewer"] == actor["id"]
    assert record["reviewer_identity"] == actor
    events = checker.get(f"/invoices/{invoice_id}/events").json()
    approval_event = next(e for e in events if e["type"] == "HUMAN_APPROVAL_RECORDED")
    assert approval_event["payload"]["reviewer_identity"] == actor
    assert desk[0].state.store.verify_audit_chain()["ok"]


def test_revocation_invalidates_sessions_future_logins_and_prior_approvals(desk):
    invoice_id, body = reviewed_invoice(desk)
    checker = signed(desk, "approver")
    assert checker.post(f"/invoices/{invoice_id}/approval", json=body).status_code == 200
    admin = signed(desk, "admin")
    assert admin.post("/auth/revoke", json={"subject": "approver"}).status_code == 200
    assert checker.get("/invoices").status_code == 401
    assert sign_in(desk, "approver")[1].status_code == 403
    payer = signed(desk, "payer")
    assert payer.post(f"/invoices/{invoice_id}/payment").status_code == 409
    assert desk[0].state.store.get_payment(invoice_id) is None


@pytest.mark.parametrize("subject", ["approver", "payer"])
def test_revocation_during_signing_blocks_atomic_payment_authorization(desk, monkeypatch, subject):
    invoice_id, body = reviewed_invoice(desk)
    checker = signed(desk, "approver")
    assert checker.post(f"/invoices/{invoice_id}/approval", json=body).status_code == 200
    workflow = desk[0].state.workflow
    original = workflow.signer.sign
    def revoke_then_sign(permit):
        workflow.store.auth_revoke_subject(desk[1].oidc_issuer, subject, "admin", int(time.time()))
        return original(permit)
    monkeypatch.setattr(workflow.signer, "sign", revoke_then_sign)
    response = signed(desk, "payer").post(f"/invoices/{invoice_id}/payment")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "payment_authorization_conflict"
    assert workflow.store.get_payment(invoice_id) is None
    assert workflow.payment_provider.submission_calls == 0


def test_logout_expiry_and_restart_preserve_correct_session_authority(desk):
    client = signed(desk, "reader")
    raw = client.cookies.get(SESSION_COOKIE)
    restarted = create_app(settings=desk[1], oidc_client=desk[2].client())
    other = TestClient(restarted, base_url="https://console.example")
    other.cookies.set(SESSION_COOKIE, raw)
    assert other.get("/invoices").status_code == 200
    assert client.post("/auth/logout").status_code == 200
    assert other.get("/invoices").status_code == 401
    client = signed(desk, "reader")
    with desk[0].state.store._connect() as connection:
        connection.execute("UPDATE auth_sessions SET expires_at=?", (int(time.time()),))
    assert client.get("/invoices").status_code == 401


def test_oidc_configuration_disallows_insecure_urls_and_mixed_approval_roles(desk):
    kwargs = desk[1].model_dump()
    for overrides in [
        {"oidc_issuer": "http://identity.example"},
        {"oidc_redirect_uri": "https://console.example/elsewhere"},
        {"oidc_subject_roles": {"person": ("operator", "approver")}},
        {"oidc_subject_roles": {"person": ("payer", "approver")}},
        {"oidc_subject_roles": {"person": ("admin", "approver")}},
        {"oidc_subject_roles": {}}, {"oidc_mfa_values": ()},
        {"environment": "production", "auth_mode": "testnet_tokens"},
    ]:
        with pytest.raises(ValidationError):
            Settings(_env_file=None, **{**kwargs, **overrides})


@pytest.mark.parametrize("provider", ["circle", "local"])
def test_external_provider_does_not_inherit_unauthenticated_demo_access(tmp_path, provider):
    settings = Settings(_env_file=None, database_path=tmp_path / "closed.sqlite3",
                        payment_provider=provider, auth_mode="demo", api_key="legacy")
    client = TestClient(create_app(settings=settings))
    assert client.get("/invoices", headers={"X-API-Key": "legacy"}).status_code == 503
