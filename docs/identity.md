# Individual identity and approval roles

This is an application identity control for **one business per deployment**, not shared-host tenant
isolation or approval to hold real business funds. Arc Testnet and all existing contract limits remain
unchanged. Managed policy signing, production settlement assurance, recovery drills and validated
accounting/screening are separate release gates.

## Trust and configuration

Register a dedicated OpenID Connect authorization-code client with PKCE S256. Configure RS256
ID tokens using RSA keys of at least 2048 bits, query-mode callbacks, and an exact redirect URI.
Discovery, authorization, token and JWKS endpoints must be HTTPS on the issuer's origin.
Providers with cross-origin endpoints or other signing algorithms are deliberately unsupported,
not silently accepted. No dynamic registration or arbitrary bearer-token authentication is provided.

Configure the same settings on the API and worker. Example **placeholders**, not credentials:

```dotenv
ENVIRONMENT=production
AUTH_MODE=oidc
OIDC_ISSUER=https://identity.example.invalid/realms/business
OIDC_CLIENT_ID=tameion-business
OIDC_REDIRECT_URI=https://payables.example.invalid/auth/callback
OIDC_SUBJECT_ROLES={"idp-subject-maker":["operator"],"idp-subject-checker":["approver"],"idp-subject-payer":["payer"],"idp-subject-admin":["admin"]}
OIDC_MFA_CLAIM=amr
OIDC_MFA_VALUES=["mfa"]
OIDC_ALLOW_INSECURE_LOCALHOST=false
WORKER_AUTOPAY=false
```

Obtain exact immutable subject identifiers from your IdP through a reviewed provisioning process.
Roles come only from this server-side allowlist, never invoice prose, submitted reviewer names, or
token role claims. Put a confidential client's `OIDC_CLIENT_SECRET` in a protected secret source
when registration requires one; public clients use PKCE without a secret. Never commit credentials.
Protect the database, volume/backups and reverse-proxy logs as security-sensitive assets. Temporary
PKCE verifiers exist in the database for up to five minutes; IdP ID/access tokens are never persisted.
Do not log callback query strings, authorization headers, cookies or token-exchange bodies. Rate-limit sign-in/callback requests and cap URI/body sizes at the reverse proxy; finite database state/session limits are not a substitute for denial-of-service controls.

Enforce MFA in the IdP's policy for this client, and require fresh authentication. The default signed
`amr` proof must contain `mfa`; if your IdP uses an `acr` assurance value, configure the exact
MFA-enforced value in `OIDC_MFA_CLAIM` and `OIDC_MFA_VALUES`. Do not substitute a weak/single-factor
value just to make login pass. Application checks cannot establish that a misconfigured IdP really
challenged two factors. Validate the IdP policy before any real deployment.

Use TLS at the canonical console origin; the reverse proxy forwards that origin unchanged.
No CORS credentials or wildcard redirect origins are supported. API/worker remain private single-host
services. `ENVIRONMENT=production` requires OIDC and forbids insecure loopback exceptions; it does
**not** enable mainnet or declare the other production controls complete.
`OIDC_ALLOW_INSECURE_LOCALHOST=true` is only for explicit non-production loopback fixtures.

## Roles and attribution

All configured roles may read this business's console, evidence and operational status.

| Role | Additional authority |
| --- | --- |
| reader | None |
| operator | Create/import/link invoices, evaluate, perform setup probes and ERP writeback |
| approver | Record a reviewable exception approval |
| payer | Submit payment or reconcile an existing authorization |
| admin | Revoke a subject's application access |

Approver cannot be combined with operator, payer **or admin**. Admin is not a universal financial
superuser. Operator/payer may be combined where the business accepts that staffing model; exception
approval still requires a different identity. Trusted Supplier wallet maintenance remains a separate
privileged accounting process. MFA and staff roles never bypass missing evidence, reserve protection,
destination validation, contract limits or evidence-bound approval.

In OIDC mode the server derives the reviewer ID from the verified issuer and subject and records the
verified identity/roles with the approval in signed invoice audit evidence. Client-supplied reviewer
attribution is rejected. Invoice mutation requests also record signed authenticated **intent**, not a
claim that the requested operation succeeded. Existing state/payment events establish the outcome.
Legacy shared-token approvals are preserved as evidence but cannot promote an invoice under OIDC:
a currently authorized individual must review it again.

The worker is a trusted local service operating under deployment credentials, not a browser user or
a fabricated human approver. It may execute only the unchanged deterministic automation policy and
valid approvals. The public HTTP API provides no worker bypass or client-credentials grant.

## Sessions and revocation

Authorization state, nonce and PKCE are single-use and browser-bound. An independent HttpOnly browser
secret binds login state; copying the state from a redirect/log cannot create a session.
Signature, issuer, audience, authorized party, time, nonce, authentication freshness and MFA are
verified. Optional access-token hashes are checked when supplied. The server stores only hashes of
opaque application sessions, not reusable IdP tokens.

HTTPS login/session cookies use the `__Host-` prefix (Secure, host-only, path=/), HttpOnly and SameSite=Lax. Insecure loopback fixtures use distinct development cookie names. JavaScript retains only a CSRF proof
and permissions in memory; neither identity tokens nor session credentials enter browser storage.
Mutation requests require the CSRF header and the exact configured Origin. Responses are no-store.
The default session/fresh-authentication lifetime is at most 900 seconds and no longer than the
ID token's remaining lifetime. `OIDC_SESSION_SECONDS` and `OIDC_MAX_AUTH_AGE_SECONDS` are bounded to
60–3600 seconds. There is no silent refresh or offline access.

Sign out deletes the durable application session. A requesting payer's session expiry/logout is also rechecked at the atomic new-authorization write. An MFA-authenticated admin may call:
`POST /auth/revoke` with `{"subject":"exact-idp-subject"}`, the normal session cookie,
`X-CSRF-Token` from `GET /auth/session`, and the canonical Origin. The request must originate from
the authorized browser/session; shared API tokens and metrics credentials cannot revoke anyone.

Revocation survives restart, terminates all application sessions for that subject and prevents future
sign-ins, approvals and new payment authorizations using revoked approvers/requesting payers. The
authorization transaction rechecks revocation, including when it changes during signing. Previously
committed, broadcast or on-chain authorizations (including already issued permits) cannot be undone by
logout/revocation; reconcile them, do not replay. Stop the worker process, not merely new autopay, when
containing an incident; already accepted/on-chain operations still need controlled reconciliation.
Revocation records retain the administrator's stable identity. There is deliberately no public
self-unrevocation endpoint; reinstatement requires a reviewed administrative migration/new identity.

IdP-only logout/deactivation does **not** immediately revoke existing application cookies. Your
offboarding/incident procedure must call application revocation before disabling the IdP account;
otherwise the bounded application session can remain valid until expiry. Removing roles from the
allowlist requires coordinated API/worker configuration/restart. Revoke first when removing an
approver so a running worker cannot use an old approval during the deployment window.

A separate `METRICS_API_KEY` may authenticate read-only scraping, including Bearer format; it cannot
authenticate staff routes or take financial actions. OIDC ignores `API_KEY` and `APPROVAL_TOKEN`.

## Deployment, migration and rollback

1. Keep the API private and automation stopped. Back up the **current** database and signed evidence.
2. Configure a real IdP/client, MFA policy, exact HTTPS origin and independent staff subjects/roles.
   Review least-privilege assignments out of band; do not provision one shared checker account.
3. Deploy the API and worker together with identical configuration. Migrations
   `007_identity_sessions.sql` and `008_login_browser_binding.sql` add identity state/session/revocation
   tables and an independent browser binding. Old outstanding sign-in states are invalidated, not
   trusted or upgraded. Existing financial/audit history is not rewritten.
4. Verify sign-in, absent/stale/single-factor MFA rejection, each forbidden role action, CSRF/origin
   rejection, explicit approver attribution, logout, app revocation and worker approval invalidation
   against the selected real provider. Confirm alert/incident owners can actually revoke staff.
5. Remove shared staff credentials and browser-stored legacy tokens. The OIDC console clears those
   legacy tab values and presents individual sign-in. Enable no real payments as part of this rollout.

Rollback must first stop worker automatic spending and close API ingress. Preserve the latest database,
revocations and all settlement evidence. Do not restore an older database over newer financial history,
replay a recorded plan, delete identity tables, or downgrade an exposed deployment to shared-token
authentication. An isolated mock/testnet rollback may ignore additive identity tables, but that is a
downgrade of identity guarantees and is not a production recovery path. Pending settlements still need
controlled reconciliation. A safe identity rollback for a real deployment is a frozen service until a
reviewed OIDC-capable release is restored.

`AUTH_MODE=demo` opens only actual mock accounting/payment adapters. External providers fail closed.
Existing isolated testnet tooling may explicitly use `AUTH_MODE=testnet_tokens` with both shared tokens
and a local/test/testnet/development environment. This is a migration aid, not production identity.

## Verification and remaining release gates

```sh
env AUTH_MODE=demo PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock \
  SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics API_KEY= APPROVAL_TOKEN= \
  uv run --frozen pytest -q tests/test_oidc_auth.py
nix shell nixpkgs#chromium nixpkgs#agent-browser --command bash script/oidc-console-smoke.sh
```

The browser smoke harness uses a loopback-only, explicitly opted-in local test IdP with real RSA
signatures and **simulated MFA**, mock accounting/payments and a fresh disposable database. It is not
a real provider, live payment or demonstration of real MFA enforcement.

Before release: select/register the real IdP, exercise its actual MFA/freshness/deprovisioning policy,
perform independent identity/security review, and complete the separate signing, production settlement,
recovery and accounting/screening work. Passing these tests alone is not production assurance.
