# Managed policy signing and key recovery

The policy signer is the one key that turns a decision into an authorization. It is separate from
the payer: `PaymentGuard` executes only a transfer described by a permit this key signed, and the
guard's `policySigner` is immutable. This PR moves that key from the environment onto a PKCS#11
token and documents key rotation, compromise recovery and historical audit verification.

It does **not** make the deployment production-ready. Arc Testnet enforcement, USDC-only settlement
and every existing contract limit are unchanged. Independent security review, real-HSM exercises,
production settlement assurance, restore drills and validated accounting/screening remain separate
release gates. No live payment was executed while building this.

## Two backends, one boundary

| `SIGNER_BACKEND` | Key location | Intended use |
| --- | --- | --- |
| `env` | `PERMIT_SIGNING_PRIVATE_KEY` in the process environment | testnet, local development, the existing demo |
| `pkcs11` | A non-exportable key on a PKCS#11 token | a real HSM in production; SoftHSM2 as a test double |
| `kms` | — | deliberately unimplemented; fails closed |

Both implement the same `PermitSigner` boundary, so the policy, the guard, the approval rules and
the audit chain do not change. `SIGNER_BACKEND` is validated as a closed set: an unknown value is
refused rather than treated as `env`.

## How the PKCS#11 backend signs

The private scalar never leaves the token. Signing is:

1. Build the EIP-712 digest locally: `keccak256(0x19 || 0x01 || domainSeparator || structHash)`, the
   same digest the guard recomputes on chain.
2. Call `C_Sign` with `CKM_ECDSA` over that 32-byte digest. The token returns raw `r || s`.
3. Enforce Ethereum's **low-s** form (`s <= secp256k1n/2`) by replacing `s` with `n - s` when needed.
4. Recover the y-parity by trial recovery against the token's own public key, and emit
   `r || s || v` with `v ∈ {27, 28}`.

Steps 2–4 exist because a PKCS#11 token returns neither a recovery id nor Ethereum's canonical
form. Recovery is checked against the key the deployment is pinned to, so a signature that does not
match the configured signer is refused instead of broadcast.

The signer opens one short-lived, read-only, logged-in session per signature. No session or object
handle is cached, so there is no shared mutable state to race; a 128-signature stress test across 16
threads on one signer produced 128 distinct, verifiable, low-s signatures.

Two Cryptoki details are required for that to be safe, and both were found by that test:

* The library is initialized **once per process, under a lock, with `CKF_OS_LOCKING_OK`**. Without
  that flag a PKCS#11 library may assume the *application* serialises every call, and concurrent
  `C_Sign` then corrupts memory instead of failing cleanly.
* Login is **token-wide, not per-session**. A second concurrent signer gets
  `CKR_USER_ALREADY_LOGGED_IN`, which is success, and the session close therefore does **not** call
  `C_Logout`: logging out would pull the token out from under another thread's authenticated
  session. Cryptoki logs the user out when the last session closes, so an idle process does not
  leave the token logged in.

### Fail-closed key requirements

Startup refuses to construct a signer unless the located key is:

* **secp256k1** (`CKA_EC_PARAMS` equals OID `1.3.132.0.10`), not P-256 or any other curve;
* **`CKA_SIGN` true** and usable for signing;
* **`CKA_SENSITIVE` true**;
* **`CKA_EXTRACTABLE` false**;
* and its `CKA_VALUE` is genuinely unavailable. If the token will hand back the private scalar, the
  key is refused rather than used.

An ambiguous key (more than one object for the label/id) is refused, never guessed. A missing token,
a wrong PIN, or several tokens without `PKCS11_SLOT` pinned also refuses. Every refusal is a hard
error: there is no fallback to an environment key.

## Configuration

```dotenv
SIGNER_BACKEND=pkcs11
# Path to the PKCS#11 provider library. Example (SoftHSM2 test double only):
# PKCS11_LIB_PATH=/usr/lib/softhsm/libsofthsm2.so
PKCS11_LIB_PATH=
PKCS11_SLOT=
PKCS11_KEY_LABEL=policy-sign
PKCS11_KEY_ID_HEX=01
PKCS11_USER_PIN=
# The address this key must have. Strongly recommended: catches a wrong or swapped key at start-up.
PERMIT_SIGNING_ADDRESS=
# Previous policy addresses whose historical audit signatures must still verify after a rotation.
PERMIT_SIGNING_RETIRED_ADDRESSES=
```

`PKCS11_USER_PIN` is a secret (`repr=False`, never logged, never echoed by `/setup`, which reports
only that it is set). `PERMIT_SIGNING_ADDRESS` is the address of the deployed guard's `policySigner`,
and a mismatch is a start-up failure. `PERMIT_SIGNING_ADDRESS` is also what read-only tools
(`arc-payables-verify-arc`, `/setup` live checks) compare against the on-chain guard, so those checks
never need to open the token.

`PKCS11_TEST_BACKEND=true` is a documentation marker for SoftHSM2-based development and is refused
by the factory outright. It exists to make "this is not a real HSM" explicit rather than implicit.

`ENVIRONMENT=production` refuses `SIGNER_BACKEND=env` and requires `PERMIT_SIGNING_ADDRESS`. That is
a configuration rule, not a claim of production readiness: the other production gates below still
apply, and nothing here enables mainnet.

## Rotation and compromise recovery

`policySigner` is immutable in the guard, so **rotating the policy key requires deploying a new
guard** with the new signer and moving the treasury/budget to it. Application-side, rotation is:

1. Stop the worker. Rotation is a maintenance action, never a hot swap while payments may be in
   flight. Pause the old guard if that is the agreed control.
2. Generate the new key on the HSM. Required attributes: `CKA_SIGN=true`, `CKA_SENSITIVE=true`,
   `CKA_EXTRACTABLE=false`, curve secp256k1. Keep a token backup/restore procedure for the HSM
   itself; that is HSM-vendor work, not application work.
3. Deploy the new guard bound to the new signer, with an explicit budget.
4. Set `PKCS11_KEY_LABEL`/`PKCS11_KEY_ID_HEX` to the new key and `PERMIT_SIGNING_ADDRESS` to its
   address. Move the previous address into `PERMIT_SIGNING_RETIRED_ADDRESSES`.
5. Restart the API and worker with the same configuration and restart the worker's automatic
   spending only once verification passes.
6. Reconcile anything `NEEDS_RECONCILIATION`. A permit already signed, broadcast or settled cannot
   be un-signed by rotating a key; only the chain shows what actually happened.

**Historical verification is the reason `PERMIT_SIGNING_RETIRED_ADDRESSES` exists.** The audit chain
is signed with the policy key, and `verify_audit_chain` checks each entry against the *configured*
signer, not the signer recorded on the entry. Without naming the retired address, every pre-rotation
entry would report `signature_is_not_from_the_configured_signer` — which is also exactly what a
forgery looks like, so silently accepting old signatures would break the control. Naming the retired
address keeps the history verifiable while still refusing any signer that is neither current nor
explicitly retired.

**Compromise recovery is different in kind.** If the policy key itself is believed compromised:

* Treat any permit the key could have signed as suspect. Rotate the guard immediately; do not wait
  for a maintenance window.
* Do **not** add the compromised address to `PERMIT_SIGNING_RETIRED_ADDRESSES`. Retiring an address
  means "I still trust what this key signed"; a compromised key must not be trusted, so its entries
  are expected to fail verification and the operator must record that history as untrusted rather
  than re-anchor it.
* Reconcile on-chain state and the ledger for every authorization signed during the exposure
  window. The guard's budget caps bound the blast radius; they do not undo a settled transfer.
* Preserve the database and the audit log before touching anything else, and keep the incident
  record.

A signed entry that no longer verifies must never be "fixed" by re-signing history with the new key.
The chain is evidence; rewriting it destroys the only thing it is for.

## Verification

```sh
# Unit and integration coverage against a real SoftHSM2 token; skips cleanly without one.
nix shell nixpkgs#softhsm nixpkgs#opensc --command bash script/pkcs11-softhsm-smoke.sh

# The whole suite, including the rotation/compromise tests, which need no token at all.
env PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock SCREENING_PROVIDER=fixture \
  DECISION_LAYER=heuristics API_KEY= APPROVAL_TOKEN= uv run --frozen pytest -q
```

The SoftHSM2 tests create their own token in a temporary directory, generate a secp256k1 key, and
assert real signature properties (low-s, `v` parity, tamper rejection), attribute checks (curve,
sensitivity, extractability), factory fail-closed paths, and that an extractable or wrong-curve key
is refused. They are gated behind `TAMEION_TEST_PKCS11=1` so the default suite stays hermetic.

SoftHSM2 is a **software-backed behavior double**. Passing these tests shows the PKCS#11 path is
implemented correctly; it does not demonstrate hardware isolation, tamper resistance, or that a real
HSM is configured as required. Those belong to deployment validation and independent review.

## Deployment, migration and rollback

* API and worker must run the **same** `SIGNER_BACKEND` and token configuration. A worker that cannot
  reach the token refuses to spend, which is the intended failure.
* Hosts need the PKCS#11 provider library installed and the token reachable. That is an operational
  dependency, not a Python dependency: this code uses the standard library's `ctypes` and adds no
  package.
* Switching backend is a restart, not a migration. No schema changes and no audit rewrite.
* Rollback restores the previous deployment *and its key configuration*. Because the guard pins the
  signer, an old guard accepts only the old key: roll the guard and the signer together, or the
  payment will be refused on chain. Never roll back by pointing a new guard at a retired key.
* Back up the database and the audit log before a rotation; keep the HSM backup procedure with the
  HSM vendor.
