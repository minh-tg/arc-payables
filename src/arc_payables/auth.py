"""OIDC code+PKCE browser sessions. No IdP tokens or client-chosen roles enter the ledger."""
from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from urllib.parse import quote_plus, urlencode, urlsplit

import httpx
import jwt
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

SESSION_COOKIE = "__Host-arc_payables_session"
LOGIN_COOKIE = "__Host-arc_payables_login"
DEV_SESSION_COOKIE = "arc_payables_session"
DEV_LOGIN_COOKIE = "arc_payables_login"
ROLE_PERMISSIONS = {
    "reader": {"read"},
    "operator": {"read", "operate"},
    "approver": {"read", "approve"},
    "payer": {"read", "pay"},
    "admin": {"read", "admin"},
}
MUTATIONS = {
    "create_invoice": "operate", "import_invoice": "operate", "evaluate": "operate",
    "link_invoice": "operate", "approve": "approve", "submit_payment": "pay",
    # Existing reconcile route can initiate a payment; it must also require a payer.
    "reconcile_payment": "pay", "retry_erp_writeback": "operate", "rescreen": "operate",
    "start_demo": "operate", "setup_checks": "read", "verify_payment_endpoint": "read",
}


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def fail(status: int, code: str) -> HTTPException:
    return HTTPException(status_code=status, detail={"code": code, "message": {
        "identity_required": "Sign in with the configured identity provider.",
        "forbidden": "Your verified identity does not have permission for this action.",
        "csrf_invalid": "Refresh your signed-in session before retrying this action.",
        "oidc_unavailable": "Identity verification is unavailable; access remains blocked.",
        "oidc_invalid": "The identity provider response could not be verified.",
        "login_state_invalid": "This sign-in request expired or was already used.",
    }.get(code, "Identity access remains blocked.")})


@dataclass(frozen=True)
class Identity:
    issuer: str
    subject: str
    roles: tuple[str, ...]

    @property
    def id(self) -> str:
        return "oidc:" + digest(self.issuer + "\0" + self.subject)

    @property
    def permissions(self) -> set[str]:
        return set().union(*(ROLE_PERMISSIONS[role] for role in self.roles))

    def record(self) -> dict:
        return {"id": self.id, "issuer": self.issuer, "subject": self.subject,
                "roles": list(self.roles), "mfa_verified": True}


class RevokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str = Field(min_length=1, max_length=255)


class OIDCAuth:
    def __init__(self, settings, store, client: httpx.Client | None = None):
        self.settings, self.store = settings, store
        self.client = client or httpx.Client(timeout=settings.oidc_timeout_seconds, follow_redirects=False)
        self._metadata = None
        self._keys = None
        self._fetched_at = 0.0
        self._lock = threading.Lock()

    @property
    def secure_cookie(self) -> bool:
        return urlsplit(self.settings.oidc_redirect_uri).scheme == "https"

    @property
    def session_cookie(self) -> str:
        return SESSION_COOKIE if self.secure_cookie else DEV_SESSION_COOKIE

    @property
    def login_cookie(self) -> str:
        return LOGIN_COOKIE if self.secure_cookie else DEV_LOGIN_COOKIE

    @property
    def origin(self) -> str:
        url = urlsplit(self.settings.oidc_redirect_uri)
        return url.scheme + "://" + url.netloc

    def identity(self, subject: str) -> Identity:
        roles = self.settings.oidc_subject_roles.get(subject, ())
        if not roles or self.store.auth_subject_revoked(self.settings.oidc_issuer, subject):
            raise fail(403, "forbidden")
        return Identity(self.settings.oidc_issuer, subject, tuple(roles))

    def _endpoint(self, value: str) -> str:
        # Discovery cannot send the client secret/code to an unrelated host.
        url, issuer = urlsplit(value), urlsplit(self.settings.oidc_issuer)
        if (url.scheme != issuer.scheme or url.netloc != issuer.netloc
                or url.username or url.password or url.fragment):
            raise fail(503, "oidc_unavailable")
        return value

    def configuration(self, *, refresh: bool = False) -> tuple[dict, dict]:
        with self._lock:
            elapsed = time.monotonic() - self._fetched_at
            if self._metadata is not None and elapsed < (15 if refresh else 300):
                return self._metadata, self._keys
            try:
                response = self.client.get(self.settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
                response.raise_for_status()
                metadata = response.json()
                if metadata["issuer"] != self.settings.oidc_issuer:
                    raise ValueError("Wrong discovery issuer")
                for name in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
                    self._endpoint(metadata[name])
                response = self.client.get(metadata["jwks_uri"])
                response.raise_for_status()
                keys = response.json()
                if not isinstance(keys.get("keys"), list) or not 1 <= len(keys["keys"]) <= 50:
                    raise ValueError("Invalid JWKS")
            except Exception as exc:
                # Never include URLs, codes, tokens or provider error bodies in errors.
                raise fail(503, "oidc_unavailable") from exc
            self._metadata, self._keys, self._fetched_at = metadata, keys, time.monotonic()
            return metadata, keys

    def login(self):
        metadata, _ = self.configuration()
        state, nonce, verifier, browser = (secrets.token_urlsafe(32) for _ in range(4))
        self.store.auth_save_login(digest(state), digest(nonce), verifier, digest(browser), int(time.time()))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"response_type": "code", "client_id": self.settings.oidc_client_id,
                  "redirect_uri": self.settings.oidc_redirect_uri, "scope": "openid",
                  "state": state, "nonce": nonce, "code_challenge": challenge,
                  "code_challenge_method": "S256", "max_age": self.settings.oidc_max_auth_age_seconds}
        if self.settings.oidc_mfa_claim == "acr":
            params["acr_values"] = " ".join(self.settings.oidc_mfa_values)
        url = metadata["authorization_endpoint"]
        response = RedirectResponse(url + ("&" if "?" in url else "?") + urlencode(params), status_code=302)
        response.set_cookie(self.login_cookie, browser, max_age=300, httponly=True,
                            secure=self.secure_cookie, samesite="lax", path="/")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    def _decode_identity(self, token: str, login: dict, access_token: str | None):
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or not isinstance(header.get("kid"), str):
                raise ValueError("Unsupported JWT")
            _, keys = self.configuration()
            candidates = [k for k in keys["keys"] if k.get("kid") == header["kid"]]
            if not candidates:
                _, keys = self.configuration(refresh=True)
                candidates = [k for k in keys["keys"] if k.get("kid") == header["kid"]]
            if len(candidates) != 1:
                raise ValueError("Unknown or ambiguous key")
            key = candidates[0]
            if key.get("kty") != "RSA" or key.get("use", "sig") != "sig" or key.get("alg", "RS256") != "RS256":
                raise ValueError("Wrong key purpose")
            public_key = jwt.PyJWK.from_dict(key).key
            if public_key.key_size < 2048:
                raise ValueError("Weak signing key")
            claims = jwt.decode(token, public_key, algorithms=["RS256"],
                                issuer=self.settings.oidc_issuer, audience=self.settings.oidc_client_id,
                                leeway=self.settings.oidc_clock_skew_seconds,
                                options={"require": ["iss", "aud", "sub", "exp", "iat", "nonce", "auth_time"]})
            now = int(time.time())
            for name in ("iat", "exp", "auth_time"):
                if type(claims[name]) is not int:
                    raise ValueError("Non-integer date")
            if (not isinstance(claims["sub"], str) or not 1 <= len(claims["sub"]) <= 255
                    or not isinstance(claims["nonce"], str)
                    or not hmac.compare_digest(digest(claims["nonce"]), login["nonce_hash"])
                    or claims["exp"] <= now or claims["exp"] <= claims["iat"]
                    or claims["auth_time"] > now + self.settings.oidc_clock_skew_seconds
                    or now - claims["auth_time"] >= self.settings.oidc_max_auth_age_seconds):
                raise ValueError("Invalid identity claims")
            audience = claims["aud"]
            if isinstance(audience, list) and len(audience) > 1 and claims.get("azp") != self.settings.oidc_client_id:
                raise ValueError("Unbound authorized party")
            if "azp" in claims and claims["azp"] != self.settings.oidc_client_id:
                raise ValueError("Wrong authorized party")
            proof = claims.get(self.settings.oidc_mfa_claim)
            if self.settings.oidc_mfa_claim == "amr":
                valid_mfa = isinstance(proof, list) and bool(set(self.settings.oidc_mfa_values) & set(proof))
            else:
                valid_mfa = isinstance(proof, str) and proof in self.settings.oidc_mfa_values
            if not valid_mfa:
                raise ValueError("MFA not verified")
            if "at_hash" in claims:
                if not isinstance(access_token, str):
                    raise ValueError("Missing access token")
                expected = base64.urlsafe_b64encode(hashlib.sha256(access_token.encode()).digest()[:16]).rstrip(b"=").decode()
                if not hmac.compare_digest(expected, claims["at_hash"]):
                    raise ValueError("Wrong access-token hash")
            return self.identity(claims["sub"]), claims
        except HTTPException:
            raise
        except Exception as exc:
            raise fail(401, "oidc_invalid") from exc

    def callback(self, request: Request, code: str | None, state: str | None):
        cookie = request.cookies.get(self.login_cookie)
        if not state or not code or not cookie:
            raise fail(401, "login_state_invalid")
        login = self.store.auth_consume_login(digest(state), digest(cookie), int(time.time()))
        if login is None:
            raise fail(401, "login_state_invalid")
        metadata, _ = self.configuration()
        data = {"grant_type": "authorization_code", "code": code, "client_id": self.settings.oidc_client_id,
                "redirect_uri": self.settings.oidc_redirect_uri, "code_verifier": login["verifier"]}
        secret = self.settings.oidc_client_secret
        method = "client_secret_basic" if secret else "none"
        if method not in metadata.get("token_endpoint_auth_methods_supported", ["client_secret_basic"]):
            raise fail(503, "oidc_unavailable")
        try:
            response = self.client.post(metadata["token_endpoint"], data=data,
                                        auth=httpx.BasicAuth(quote_plus(self.settings.oidc_client_id), quote_plus(secret)) if secret else None)
            response.raise_for_status()
            tokens = response.json()
            token = tokens["id_token"]
            if not isinstance(token, str) or len(token) > 32768:
                raise ValueError("Invalid ID token")
        except Exception as exc:
            raise fail(503, "oidc_unavailable") from exc
        identity, claims = self._decode_identity(token, login, tokens.get("access_token"))
        now = int(time.time())
        session = secrets.token_urlsafe(32)
        expires = min(claims["exp"], now + self.settings.oidc_session_seconds,
                      claims["auth_time"] + self.settings.oidc_max_auth_age_seconds)
        try:
            self.store.auth_create_session(digest(session), identity.issuer, identity.subject,
                                           claims["auth_time"], expires, now)
        except ValueError as exc:
            raise fail(403, "forbidden") from exc
        response = RedirectResponse("/console/", status_code=303)
        response.set_cookie(self.session_cookie, session, max_age=expires - now, httponly=True,
                            secure=self.secure_cookie, samesite="lax", path="/")
        response.delete_cookie(self.login_cookie, path="/", secure=self.secure_cookie, httponly=True, samesite="lax")
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @staticmethod
    def csrf(session: str) -> str:
        return hmac.new(session.encode(), b"arc-payables-csrf-v1", hashlib.sha256).hexdigest()

    def authenticate(self, request: Request) -> Identity:
        session = request.cookies.get(self.session_cookie, "")
        record = self.store.auth_get_session(digest(session), int(time.time())) if session else None
        if record is None or record["issuer"] != self.settings.oidc_issuer:
            raise fail(401, "identity_required")
        identity = self.identity(record["subject"])
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            presented = request.headers.get("X-CSRF-Token", "")
            if (request.headers.get("Origin") != self.origin
                    or not hmac.compare_digest(presented.encode(), self.csrf(session).encode())):
                raise fail(403, "csrf_invalid")
        request.state.identity = identity
        request.state.session_hash = digest(session)
        return identity

    def authorize(self, request: Request, permission: str) -> Identity:
        identity = self.authenticate(request)
        if permission not in identity.permissions:
            raise fail(403, "forbidden")
        return identity


def mount_auth(app, auth: OIDCAuth | None, mode: str):
    @app.get("/auth/config", tags=["identity"])
    def auth_config():
        return JSONResponse({"mode": mode, "login_url": "/auth/login" if auth else None},
                            headers={"Cache-Control": "no-store"})

    @app.get("/auth/login", tags=["identity"])
    def auth_login():
        if auth is None:
            raise fail(503, "identity_required")
        return auth.login()

    @app.get("/auth/callback", tags=["identity"])
    def auth_callback(request: Request, code: str | None = None, state: str | None = None):
        if auth is None:
            raise fail(503, "identity_required")
        return auth.callback(request, code, state)

    @app.get("/auth/session", tags=["identity"])
    def auth_session(request: Request):
        if auth is None:
            raise fail(401, "identity_required")
        identity = auth.authenticate(request)
        return JSONResponse({"identity": identity.record(), "permissions": sorted(identity.permissions),
                             "csrf_token": auth.csrf(request.cookies[auth.session_cookie])},
                            headers={"Cache-Control": "no-store"})

    @app.post("/auth/logout", tags=["identity"])
    def auth_logout(request: Request):
        if auth is None:
            raise fail(401, "identity_required")
        auth.authenticate(request)
        auth.store.auth_delete_session(digest(request.cookies[auth.session_cookie]))
        response = JSONResponse({"logged_out": True}, headers={"Cache-Control": "no-store"})
        response.delete_cookie(auth.session_cookie, path="/", secure=auth.secure_cookie, httponly=True, samesite="lax")
        return response

    @app.post("/auth/revoke", tags=["identity"])
    def auth_revoke(request: Request, body: RevokeInput):
        if auth is None:
            raise fail(401, "identity_required")
        actor = auth.authorize(request, "admin")
        if body.subject not in auth.settings.oidc_subject_roles:
            raise fail(404, "forbidden")
        auth.store.auth_revoke_subject(auth.settings.oidc_issuer, body.subject, actor.id, int(time.time()))
        return JSONResponse({"revoked": True}, headers={"Cache-Control": "no-store"})
