"""Credential-free IdP fixture: real RSA signatures, code binding and PKCE checks."""
import base64
import hashlib
import json
import secrets
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa


class FakeOIDC:
    def __init__(self, issuer="https://identity.example", client_id="tameion-test"):
        self.issuer, self.client_id = issuer, client_id
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key"
        self.codes = {}
        self.requests = []
        self.claim_overrides = {}
        self.algorithm = "RS256"
        self.signing_key_override = None
        self.claims_to_drop = set()
        self.metadata_overrides = {}
        self.unavailable = False

    def jwks(self):
        key = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        key.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [key]}

    def metadata(self):
        return {"issuer": self.issuer, "authorization_endpoint": self.issuer + "/authorize",
                "token_endpoint": self.issuer + "/token", "jwks_uri": self.issuer + "/jwks",
                "token_endpoint_auth_methods_supported": ["none", "client_secret_basic"],
                "code_challenge_methods_supported": ["S256"], **self.metadata_overrides}

    def code_for(self, login_url, subject):
        params = parse_qs(urlsplit(login_url).query)
        code = secrets.token_urlsafe(24)
        self.codes[code] = {"subject": subject, **{k: v[0] for k, v in params.items()}}
        return code, params["state"][0]

    def exchange(self, data):
        record = self.codes.pop(data.get("code"), None)
        if not record:
            return 400, {"error": "invalid_grant"}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(data.get("code_verifier", "").encode()).digest()).rstrip(b"=").decode()
        if (challenge != record["code_challenge"] or data.get("redirect_uri") != record["redirect_uri"]
                or data.get("client_id") != self.client_id):
            return 400, {"error": "invalid_grant"}
        now = int(time.time())
        claims = {"iss": self.issuer, "aud": self.client_id, "sub": record["subject"],
                  "iat": now, "auth_time": now, "exp": now + 900, "amr": ["pwd", "mfa"],
                  "nonce": record["nonce"], **self.claim_overrides}
        for name in self.claims_to_drop:
            claims.pop(name, None)
        key = (self.signing_key_override or self.key) if self.algorithm == "RS256" else "not-an-rsa-key" * 3
        token = jwt.encode(claims, key, algorithm=self.algorithm, headers={"kid": self.kid})
        return 200, {"id_token": token, "access_token": "test-access-token", "token_type": "Bearer"}

    def transport(self, request):
        self.requests.append(request)
        if self.unavailable:
            return httpx.Response(503, json={"error": "fixture unavailable"})
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(200, json=self.metadata())
        if path.endswith("/jwks"):
            return httpx.Response(200, json=self.jwks())
        if path.endswith("/token"):
            data = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            status, body = self.exchange(data)
            return httpx.Response(status, json=body)
        return httpx.Response(404)

    def client(self):
        return httpx.Client(transport=httpx.MockTransport(self.transport))


def fixture_app():
    """Explicitly opt-in, loopback-only fixture for the browser smoke script."""
    import os
    from html import escape
    from urllib.parse import urlencode
    from fastapi import FastAPI, Request
    from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

    issuer = os.environ.get("OIDC_FIXTURE_ISSUER", "")
    redirect_uri = os.environ.get("OIDC_FIXTURE_REDIRECT", "")
    if (os.environ.get("OIDC_TEST_FIXTURE") != "1" or not issuer.startswith("http://127.0.0.1:")
            or not redirect_uri.startswith("http://127.0.0.1:")):
        raise RuntimeError("This test-only IdP requires explicit loopback fixture configuration.")
    provider = FakeOIDC(issuer)
    app = FastAPI()

    @app.get("/.well-known/openid-configuration")
    def metadata():
        return provider.metadata()

    @app.get("/jwks")
    def jwks():
        return provider.jwks()

    @app.get("/authorize")
    def authorize(request: Request, subject: str | None = None):
        params = dict(request.query_params)
        roles = ("reader", "operator", "approver", "payer", "admin")
        if params.get("redirect_uri") != redirect_uri or params.get("client_id") != provider.client_id:
            return JSONResponse({"error": "invalid_client"}, status_code=400)
        if subject is None:
            links = "".join('<p><a data-subject="' + role + '" href="' +
                            escape("/authorize?" + urlencode({**params, "subject": role}), quote=True) +
                            '">' + role + "</a></p>" for role in roles)
            return HTMLResponse("<h1>TEST ONLY · Local identity fixture</h1><p>MFA is simulated, not a real identity-provider exercise.</p>" + links)
        if subject not in roles:
            return JSONResponse({"error": "invalid_subject"}, status_code=400)
        code, state = provider.code_for(str(request.url), subject)
        return RedirectResponse(redirect_uri + "?" + urlencode({"code": code, "state": state}), status_code=302)

    @app.post("/token")
    async def token(request: Request):
        data = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
        status, body = provider.exchange(data)
        return JSONResponse(body, status_code=status)

    return app
