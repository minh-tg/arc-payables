# Arc Payables

An accounts payable agent for Arc Testnet. It reads supplier invoices, checks them against the
accounting system, and pays them in USDC.

Three rules shape the code:

1. **The destination comes from the supplier record.** A payee address printed on the invoice is
   evidence. It is never used as a payment destination, and no approval can substitute another
   address.
2. **The amount is bounded by a contract.** Transfers go through `PaymentGuard`, deployed with
   spending caps fixed at deployment. A bug in this service, or a compromised process, cannot spend
   past them.
3. **The record is append-only.** Each decision is appended to a per-invoice hash chain and signed,
   so the history can be checked later instead of trusted.

The agent proposes a payment. It cannot authorize one: the permit it needs is signed by a separate
key held only by the payment service, and the guard executes only the transfer that permit
describes.

Most agent payment projects keep the spending limit in the service that calls the wallet API. Move
it into a contract and the failure mode changes, because the worst a bad decision can then do is
spend what the contract already permitted.

## What it does

Given a Purchase Invoice in ERPNext, it:

- imports the invoice and matches amount, currency, supplier, line items and links against the payable
- checks the purchase order and receipt behind it, plus duplicates, screening, due date, amount limit
  and treasury reserve
- pays if every check passes, or escalates to a human with the reason
- writes the result back as a Payment Entry, expensing the network fee separately so the supplier
  receives the authorized amount exactly

It also orders the queue when the balance cannot cover every invoice (`GET /plan`), reports where the
balance stops covering what is due (`GET /forecast`), and re-screens counterparties on a schedule so
a change in risk blocks later payments (`GET /suppliers`).

A console at `/console` reads all of it. No screen is available only in the browser; each one calls
the documented API.

**Scope:** Arc Testnet, USDC only, no mainnet. What has been verified against live systems, and what
has not, is in [Security and integration limitations](#security-and-integration-limitations).
[HISTORY.md](HISTORY.md) records where the project started.

## What is implemented

- FastAPI endpoints with generated OpenAPI at `/docs` and `/openapi.json`.
- SQLite migrations, explicit workflow states, append-only event history, invoice/payment uniqueness, request idempotency, and durable payment/ERP links.
- Evidence comparison for Supplier, Purchase Invoice, PO lines, Purchase Receipt quantities, duplicates, invoice payee, address screening, due dates/terms, early discounts, amount limits, and treasury reserve.
- `AccountingConnector`, `PaymentProvider`, `EvidenceStore`, `DecisionAgent`, and `CurrencyConverter` boundaries, with local mocks and vendor adapters.
- Circle Developer-Controlled Wallets adapter and a Foundry one-time payment guard. The real payment provider targets only `ARC-TESTNET` and verifies chain ID `5042002` before reads or writes.
- Frappe/ERPNext REST API v1 adapter. It creates/submits a standard Payment Entry only after Circle reports `COMPLETE`; ERP writeback retry never resubmits a blockchain payment.
- Mock seed data: an eligible supplier invoice and a suspicious invoice with an attacker payee, amount/PO mismatch, and insufficient receipt evidence, each backed by a separate simulated ERP record, plus an importable ERP invoice for the `POST /invoices/import` path.

## Arc/Circle path and security

Two executors implement the same payment path: an exact `approve`, then the policy-signed `PaymentGuard.pay`. The flow can be exercised either way, and the authorization rules do not depend on which one runs:

* **Circle Developer-Controlled Wallets** (`PAYMENT_PROVIDER=circle`) keeps the payer key at Circle. This is the production-shaped option.
* **Local key** (`PAYMENT_PROVIDER=local`) holds the payer key in the environment. It is Arc Testnet only, asserted from the node's `eth_chainId`, and exists so the path can be run and developed without Circle credentials. The guard's budget caps and exact-allowance behavior are identical.

Both refuse to proceed on an unconfigured or incomplete setup, and neither can exceed the guard's on-chain budget.

`forge test` includes an invariant suite that fuzzes arbitrary sequences of payments, time
movements and pauses against those caps, and checks after every call that the epoch spend stayed
within its cap, that the per-recipient figures still sum to the epoch figure, that budget consumed
matches what actually settled, and that nothing settled while paused.

The guard also has a pause control, held by an address given at deployment. It exists so an incident
can be stopped without deploying a new guard. The pauser can only stop and resume payments: it cannot
move tokens, redirect a payment, or raise a cap. Point it at a burn address if no pause authority is
wanted at all, and note that the deploy script requires it to differ from the policy signer.

Arc Canteen context and current official docs were checked before choosing the adapter:

- Arc Testnet chain ID: `5042002`; RPC: `https://rpc.testnet.arc.io`; testnet-only deployment.
- Arc's USDC ERC-20 interface: `0x3600000000000000000000000000000000000000`, 6 decimals. Arc also uses native USDC for gas at 18-decimal precision; this code uses ERC-20 6-decimal units for payment amounts and checks the native balance separately for gas.
- Circle Developer-Controlled Wallets list Arc Testnet and SCA support. The bundled Circle Wallets OpenAPI includes `ARC-TESTNET` for the contract-execution endpoint. The adapter uses that documented contract-call path, not a raw transfer or a wallet private key. It requires a Circle SCA whose wallet ID/address and chain are verified.
- `PaymentGuard` verifies an EIP-712 policy signature bound to payer, token, trusted recipient, exact amount, evidence hash, unique payment ID, expiry, chain ID, and guard contract domain. Its replay mapping consumes the unique payment ID before the exact `transferFrom`.
- Circle executes an exact ERC-20 allowance (zero-reset if needed) before the guard call; no unlimited approval. Each Circle operation has a durable UUID v4 idempotency key. An uncertain operation is inspected using Circle status and the on-chain payment ID before any retry. If it cannot be resolved, the invoice remains in `NEEDS_RECONCILIATION`.
- The model receives no signing credential or payment tool. The MVP contains no LLM adapter; `DecisionAgent` is deterministic and has no `PaymentProvider` reference. Invoice/OCR text is untrusted, hashed, and not retained as raw text.

Sources used: [Arc RPC endpoints](https://docs.arc.io/arc/references/rpc-endpoints), [Arc contract addresses](https://docs.arc.io/arc/references/contract-addresses), [Circle Wallets supported blockchains](https://developers.circle.com/wallets/supported-blockchains), [Circle dev-controlled transfers](https://developers.circle.com/wallets/dev-controlled/transfer-tokens-across-wallets), [Circle entity-secret sample](https://github.com/circlefin/w3s-entity-secret-sample-code), [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest), [ERPNext Payment Entry](https://docs.frappe.io/erpnext/payment-entry).

## What runs without credentials

A reviewer can run the whole workflow with no accounts, no keys and no network access. The defaults
are `PAYMENT_PROVIDER=mock`, `ACCOUNTING_PROVIDER=mock` and `SCREENING_PROVIDER=fixture`, and the
payment guard's own compiled bytecode is exercised on a local EVM and in the Foundry suite.

| Command | What it demonstrates | Credentials |
| --- | --- | --- |
| `uv run pytest -q` | The full Python suite, including the executor tests that drive the real guard | none |
| `forge build && forge test` | The guard's unit tests and invariants | none |
| `uv run arc-payables-fake-circle` | The real `PaymentGuard` bytecode on a local chain carrying Arc's chain id, behind an emulated Circle W3S API | none |
| `uv run pytest tests/test_circle_executor.py -q -k budget` | The contract refusing a payment the policy approved | none |
| seed, then the API or `/console` | Evaluate, decide, pay, write back and verify the audit chain | none |

Run against a fresh database with default settings, the seeded legitimate invoice reaches
`ELIGIBLE`/`PAY_NOW` with no failing checks and settles to `ERP_RECORDED`, the suspicious invoice
escalates on seven checks, and the audit chain verifies. Only the chain and the ledger are
simulated on that path, and every response says so.

The console is the fastest way in. Three commands, no key:

```bash
uv run arc-payables-seed                 # a payable, a blocked invoice, a treasury and a supplier
uv run uvicorn arc_payables.api:app      # API and console on 127.0.0.1:8000
# then open http://127.0.0.1:8000/console/
```

Straight after seeding the console is thin: two invoices still waiting to be looked at, an empty
settlement log, and a worker that has never run. One more command fills every screen, and it is what
the smoke script does before it renders anything:

```bash
uv run arc-payables-worker --once --autopay --intake
```

That one pass captures a payable, evaluates it against the policy, pays it through the mock
provider, writes it back to the ledger, and records the six steps it took. Nothing leaves the
machine: the demo adapters are selected unless a provider is explicitly configured.

With nothing configured, `/console/`, `/invoices`, `/plan`, `/forecast`, `/suppliers`,
`/worker/status` and `/audit/verify` all answer without a credential. That is deliberate rather than
sloppy: the demo providers hold nothing worth protecting, and the API refuses to start an external
integration at all until a key is configured, so the open path is only ever the local demo. The
console asks for a key so that the same page works against a configured deployment.

Live components need the operator's own credentials, and none are in this repository:

| Component | Needs | Without it |
| --- | --- | --- |
| Arc Testnet settlement | A funded wallet, a deployed guard, the policy signing key | The mock provider settles locally |
| ERPNext writeback | A reachable site and a scoped API user | The mock connector records locally |
| Circle Developer-Controlled Wallets | API key, entity secret, SCA wallet | The credential-free executor stands in |
| OpenSanctions screening | An API key | The fixture provider answers, and a live run escalates every invoice |

Each is configuration, not code. What the live runs produced is recorded under
[Security and integration limitations](#security-and-integration-limitations).

## Local setup and demo

Requirements: Python 3.11+, `uv`, Foundry (`forge`).

```bash
uv sync
cp .env.example .env     # optional for defaults; never commit this local file
uv run arc-payables-migrate
uv run arc-payables-seed      # seeds mock records
uv run uvicorn arc_payables.api:app --reload
uv run arc-payables-verify-arc  # optional read-only check against live Arc Testnet
```

The API defaults to `PAYMENT_PROVIDER=mock` and `ACCOUNTING_PROVIDER=mock`. No credentials are needed for evaluation or tests. The local demo thresholds are **1,000 USDC maximum unattended invoice** and **2,000 USDC minimum post-payment reserve**; these are examples, not business recommendations. The mock wallet starts with 5,000 USDC. All are configurable. Mock transaction hashes and ERP entry IDs are simulations, not actual integrations or payments.

Evaluate the seeded invoices:

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/ready
curl -s -X POST http://127.0.0.1:8000/invoices/demo-invoice-legitimate/evaluate
curl -s -X POST http://127.0.0.1:8000/invoices/demo-invoice-suspicious/evaluate
```

The first should become `ELIGIBLE` / `PAY_NOW`; the second is stopped and escalated (`ESCALATE`) because the invoice address is untrusted and disagrees with the supplier record, and the billed PO/receipt evidence does not match. A demo payment can then exercise the mock-only path:

```bash
curl -s -X POST http://127.0.0.1:8000/invoices/demo-invoice-legitimate/payment
curl -s http://127.0.0.1:8000/invoices/demo-invoice-legitimate/events
```

Imported ERPNext invoices are the payable path. The seed includes one invoice that exists in the
simulated ERP but has not been captured locally yet:

```bash
curl -s -X POST http://127.0.0.1:8000/invoices/import \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 6f1c3a15-6a6f-4a1b-9a1e-1d0f4a1e77aa' \
  -d '{"external_invoice_id":"PINV-ACME-2026-003"}'
```

**An invoice with no linked accounting record is not payable by this agent.** Paying an
unlinked invoice would mean trusting submitted invoice data with no independent accounting
record of the payable, so `accounting_invoice_match` fails closed and the invoice stops at
human review. An invoice is payable only when the connector produces a linked Purchase
Invoice whose amount, currency, supplier reference, and line items match the captured
data. This is why the manifest above is the intended payable route.

Create/import invoice requests require an `Idempotency-Key` UUID v4. Example capture (this
invoice is held until a matching accounting record exists):

```bash
curl -s -X POST http://127.0.0.1:8000/invoices \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: 2a7be542-94c9-4dfa-92fc-b7037f11a120' \
  -d '{
    "supplier_id":"SUP-ACME-001",
    "invoice_number":"ACME-INV-2026-100",
    "invoice_date":"2026-01-01",
    "due_date":"2026-01-20",
    "amount":"250.00",
    "currency":"USDC",
    "invoice_payee_address":"0x1111111111111111111111111111111111111111",
    "purchase_invoice_id":"PINV-100",
    "purchase_order_ids":["PO-ACME-2026-001"],
    "receipt_ids":["PR-ACME-2026-001"],
    "lines":[{"item_code":"INDUSTRIAL-FILTER","quantity":"10","amount":"250.00","purchase_order_line_id":"POL-ACME-2026-001-1","receipt_line_ids":["PRL-ACME-2026-001-1"]}],
    "untrusted_text":"Invoice note/OCR is accepted only as untrusted input and is not persisted."
  }'
```

API routes are listed in Swagger at `/docs`. Configure `API_KEY` before enabling Circle or Frappe adapters; write endpoints reject external-adapter operation without API auth. Human approval also requires a separate `APPROVAL_TOKEN`, and a reviewer may only acknowledge the listed exceptions, never choose a destination address. Do not put secrets in chat, command history, source control, logs, or model context.

## Sending a real Arc Testnet payment

`arc-payables-live-run` sends one payment, and refuses to guess. It checks the chain id from the
node, the deployed guard's budgets, that the destination is a **human-verified** trusted
Supplier wallet, that the invoice is linked to an acceptable accounting payable, and that the
treasury covers the amount while preserving the reserve. Nothing is signed without `--confirm`,
so the dry run is free:

```bash
uv run arc-payables-verify-arc                        # read-only: is this deployment ready, and what is missing?
uv run arc-payables-live-run --invoice <invoice-id>   # preflight only
uv run arc-payables-live-run --invoice <invoice-id> --confirm
```

Setup, in order. The first three need your keys, which never leave your machine:

```bash
arc-canteen login                                # a funded Arc Testnet wallet and an RPC URL
export RPC=$(arc-canteen rpc-url)
cast wallet new                                  # the supplier destination you control
cast wallet new                                  # the policy signer; must differ from the deployer

export DEPLOYER_PRIVATE_KEY=<arc-canteen wallet key>
export PERMIT_SIGNING_PRIVATE_KEY=<policy signer key>
export PAYMENT_GUARD_PER_PAYMENT_CAP=50000       # 0.05 USDC per payment, in 6-decimal units
export PAYMENT_GUARD_EPOCH_CAP=200000
export PAYMENT_GUARD_RECIPIENT_EPOCH_CAP=50000
export PAYMENT_GUARD_EPOCH_LENGTH_SECONDS=86400
export PAYMENT_GUARD_PAUSER=<an operator address that may pause payments>
forge script script/DeployPaymentGuard.s.sol --rpc-url $RPC --broadcast

export PAYMENT_PROVIDER=local
export LOCAL_PAYMENT_PRIVATE_KEY=<treasury payer key>
export LOCAL_PAYMENT_GUARD_ADDRESS=<guard from the deploy>
export LOCAL_PAYMENT_RPC_URL=$RPC
```

Then set `custom_usdc_wallet_address` on the demo Supplier to the supplier address and tick
`custom_usdc_wallet_verified` **in ERPNext**, by hand. That is deliberately the one step the tooling
will not do: changing a trusted payment destination is a privileged human act, and every payment
is refused until it has happened.

The demo thresholds matter here. The default reserve floor is 2,000 USDC, so either lower
`MIN_RESERVE_USDC`/`MAX_INVOICE_USDC` for a small run or fund the wallet properly; a preflight
failure says exactly which limit was hit.

## Circle platform usage

Used today, and verified live rather than asserted:

* **Developer-Controlled Wallets.** The payer is a Circle SCA (`accountType: SCA`, wallet core
  `circle_6900_singleowner_v4`) on `ARC-TESTNET`. The adapter reads the wallet back from Circle before
  it signs anything and refuses a wallet that is not the configured Arc smart account, so a
  misconfigured address fails closed instead of sending.
* **USDC** on Arc, settled through the guard.
* **Contracts.** The budget is enforced by an on-chain guard rather than by configuration, and the
  service cannot change it.
* **Gas Station, in effect.** Both Circle settlements debited the wallet exactly the payment amount
  and nothing else, and the transaction was relayed by a different address carrying a `userOpHash`:
  the wallet did not pay gas. Circle documents a preconfigured testnet Gas Station policy that
  sponsors gas for supported wallets, so this is the mechanism the observation matches. The recorded
  network fee is whatever Circle reports for the settling operation, and it is booked as our own
  cost either way.

Deliberately not used yet, with the reason each one is a decision rather than a gap:

* **USYC.** Available on Arc Testnet with published contract addresses, but restricted to entities
  that are not U.S. persons under Regulation S. Subscribing is an onboarding and eligibility step for
  the operator, not an adapter.
* **Gateway.** Reachable now, since it takes an API key, a wallet and USDC. It is not wired because a
  deposit moves treasury funds into a Gateway contract outside the guard, and adding an uncapped
  money-movement path would weaken the one claim this system rests on. It belongs behind the same
  kind of stated limit as a payment.
* **CCTP and App Kit.** Both need a second chain and a wallet on it, which is a real cross-chain
  deployment rather than a small adapter.
* **EURC.** The mapping supports one settlement currency at a time and refuses anything without an
  explicit rate, so a euro vendor is a configuration and FX question first.

Each is an adapter behind an existing port rather than a redesign, and none of them changes the
policy, the guard, or the audit chain.

## Operator console

`uv run uvicorn arc_payables.api:app` then open <http://127.0.0.1:8000/console/> and paste the API key
(and, for approvals, the approval token) into the header. It is served as static files with no build
step, and it contains no data of its own. Every call it makes is an authenticated API call, so the
same work is available with curl.

| View | What it shows |
| --- | --- |
| **Attention** | Everything waiting on a person in one place: escalations, held invoices, uncertain settlements, confirmed payments the ledger has not taken, a breached reserve, a broken audit chain, a paused guard, screenings past cadence, a failing worker. Ranked with the worst first, with the invoice behind each item, and the alerts the last pass raised. It reads the same snapshot the metrics are rendered from, so the screen and a scraper cannot disagree. |
| **Queue** | The plan's ordering with the reason for each position and the balance after each payment, the invoices that are not payable with their policy outcome, and every invoice's state with a re-evaluate action |
| **Payments** | The settlement log: every payment the agent authorized with its outcome, totals, and filters, plus a lookup for one payment by invoice id, invoice number, payment id or transaction hash. The lookup returns the whole story, and an explicit re-check asks the provider and the chain again and compares the fee it reports with the fee we booked. |
| **Invoice** | Evidence checks with their result and whether a human may override them, which layer decided with its rationale and confidence, the model/prompt/response hashes when a deliberating layer was used, missing evidence and conflicts, the audit chain with a verification button, and the actions: link, approve, pay |
| **Worker** | The last pass, step by step, with the reason each step declined or deferred something, and the alerts it raised |
| **Treasury & risk** | Forward coverage with the shortfall date, obligations in due-date order, expected inflows, and each counterparty's tier, latest screening, resulting automatic limit and open exposure, with a re-screen action |
| **Setup** | What this deployment still needs: every setting that matters, whether it is set, and what stops working without it, plus two read-only probes of the ledger and the chain |

Two things the console is deliberate about. It shows the **trusted destination** from the supplier
record beside the invoice's own payee field, marked untrusted, so the distinction the system rests
on is visible rather than implied. And it states the exact amount and destination in the payment
confirmation, because that is the moment a human is accountable for. It also never writes a setting:
credentials belong in the environment and in mode-600 files, not in a page reachable with one shared
API key. The Setup view reports a secret as set or missing and never in full, and shows a URL as its
host with the path removed, because the RPC endpoints shipped with this project carry a token there.

The look is a light neutral canvas with a dark theme beside it, one accent, semantic status colours
that always carry a label as well as a colour, monospaced identifiers and amounts, and tables that
reflow into labelled rows on a narrow screen rather than shrinking. The design system was prototyped
in OpenDesign and ported into the console's own static files: no framework, no build step, and no
external asset.

**The guided view.** A reader may never have held a stablecoin, so a new session opens with the
explanations switched on: a plain-language introduction to a view, the suggested next move against
anything that names a problem, and every domain term underlined so its definition opens where it
stands, reachable with a tap and the keyboard as well as a pointer. The definitions come from the
service's own `/explanations.json` rather than from the page, so they cannot describe the system
differently from the way it behaves, and a term with no definition behind it is caught by a test and
by `script/console-smoke.sh`. One header control turns the layer off for a reader who has learned the
vocabulary, and the choice lasts the tab. The glossary covers the money itself, USDC and what it is
for, because that is the first thing a new operator has to read and the last thing anyone thinks to
explain.

There are two layers here, and they are deliberately separate. The **plain words for a code** are
part of the console, not a beginner mode: `NEEDS_RECONCILIATION` is translated on every screen for
everyone, because reading a screen should not require decoding it first. What the guided view adds is
the rest: the terms, the introductions, and the suggested next move. Every table of words the
service publishes has to reach a screen, and a test fails when one does not, because seven of them
once sat served, tested and rendered nowhere while operators read the raw codes they explained.

`script/console-smoke.sh` renders every view in a headless browser against the seeded demo and fails
if one of them throws. The Python suite can only see that a module parses and its imports resolve,
which is not enough: a wrong destructure of an API response parses perfectly and dies at render time,
and one shipped that way until this script caught it. It drives one worker pass before rendering,
because an empty table cannot show a wrong destructure and most of the console is empty straight
after seeding, and it fails outright if that pass produced nothing. The demo adapters are pinned, so
a machine with live credentials exported cannot be made to move money by a script that exists to look
at screens. It also checks the one thing about the guided view that no server-side test can reach:
that every term a page draws has a definition behind it, because a term with no definition renders an
empty popover and throws nothing at all.

The console is shipped code, so its modules are syntax-checked in the test suite: a JavaScript
error would otherwise produce a blank page that no server-side test would catch.

## Running unattended

The worker is what makes the rest of this operate without somebody pressing a button.

```bash
uv run arc-payables-worker --once                  # one pass, for a cron or a smoke test
uv run arc-payables-worker --interval 30           # passes every 30 seconds until interrupted
uv run arc-payables-worker --interval 30 --autopay  # also pay what the policy already authorized
```

A pass does five things and stops:

| Step | What it does |
| --- | --- |
| `reconcile` | Finds settlements whose confirmation never arrived, and asks the chain again. Idempotent by design. |
| `writeback` | Finishes confirmed payments the accounting system has not taken yet, including the case where the payment entry landed and the fee entry did not. A network fee the provider could not name at settlement is asked for again rather than written off, and a failure to name it is a deferral with a backoff, never a permanent disable. |
| `intake` | Reads the payables the ledger still owes, imports the ones nobody has captured, and evaluates each against the policy. Discovery is what makes the queue current without a person. |
| `rescreen` | Re-screens counterparties past their cadence and moves their risk tier. |
| `autopay` | Pays the invoices the deterministic policy put in `ELIGIBLE` with a `PAY_NOW` decision. Off unless asked for. |
| `observe` | Reports the balance, the reserve headroom and the guard's budgets. Observation is not evidence, so this writes nothing. |

Run against a live ledger with autopay on, the whole chain runs in one pass: the pass above found
three payables nobody had imported, evaluated all three, and paid none of them, because screening has
no provider configured and the policy therefore requires a person. That refusal is the point. Nothing
in the pass can turn an escalation into a payment, and a payable it will not settle, such as an
invoice in a currency the system has no rate for, is recorded as declined with the reason rather than
counted as a failure.

**What it cannot do.** It never approves anything: an escalated invoice still needs a person, and the
worker has no approval path at all. It runs the same workflow the API serves, so a payment it starts
produces the same evidence hash, the same permit and the same audit chain as one a person starts. The
guard's caps bind it exactly as they bind anything else, and `--autopay` only pays invoices the
deterministic policy already put in `ELIGIBLE` with a `PAY_NOW` decision. Autopay is off unless asked
for, because deciding to spend unattended is a policy an operator should state rather than inherit.
Discovery is a separate switch (`--no-intake`) and is on by default, because reading what a ledger
owes and deciding it is not the same act as spending it.

**What it is built to survive.** One broken invoice does not stop the rest of the queue, one broken
step does not stop the pass, and a failed pass does not stop the loop. Every pass is recorded, so a
worker running as its own process is still visible to the API. `--max-actions` caps the work in a
single pass, and what it defers is picked up by the next one rather than lost.

### Supervised service

The single-host Podman Compose deployment runs one API and one worker against a shared SQLite volume. It defines restart policies, healthchecks, and a host-local Prometheus scrape example. See the [deployment runbook](deploy/arc-payables/README.md) for configuration, startup, health, alerts, and storage limits. The console's Worker view reads `GET /worker/status`.

### Metrics

`GET /metrics` exposes the Prometheus text format. It needs the API key, and it also accepts it as a
bearer token, because Prometheus cannot send `X-API-Key`:

```yaml
scrape_configs:
  - job_name: arc-payables
    authorization:
      credentials: <API_KEY>
    static_configs:
      - targets: ["127.0.0.1:8000"]
```

The metrics worth alerting on:

| Metric | Why |
| --- | --- |
| `arc_payables_worker_consecutive_failures` | A single degraded pass is noise; a run of them means the loop is not doing its job. |
| `arc_payables_worker_last_run_timestamp_seconds` | A worker that stopped is worse than no worker, because the operator believes it is running. |
| `arc_payables_payments_needing_attention{reason="unconfirmed"}` | Settlements nobody has confirmed. |
| `arc_payables_payments_needing_attention{reason="unrecorded"}` | Money that left but is not in the ledger. |
| `arc_payables_audit_chain_ok` | Zero means the audit chain no longer verifies, which outranks everything else here. |
| `arc_payables_reserve_headroom_usdc` | Negative means the reserve floor is already breached. |
| `arc_payables_guard_paused` | The operator stopped payments, or somebody else did. |

Labels are deliberately bounded. States, statuses, kinds and outcomes are small vocabularies;
supplier names and invoice ids are not, and a label whose value grows with the data is how a metrics
endpoint becomes a memory leak.

## Database and tests

Migrations are in `migrations/`; the service applies them on startup. The DB defaults to `data/arc_payables.sqlite3` (`DATABASE_PATH` overrides it).

```bash
uv run pytest
forge build
forge lint
forge test
```

Tests use isolated temporary databases, fake Circle/Frappe HTTP responses, and mocks; they require no credentials. They cover invoice/payee/amount tampering, prompt injection, evidence that does not match the linked ERP invoice, policy conditions, duplicates and concurrent requests, permit signature/domain/expiry/replay, payment uncertainty/failure, ERP timeouts and idempotency, Arc/RPC mismatch, and guard signature/domain/token validation.

## Optional Arc Testnet setup (not exercised here)

0. Check the hardcoded Arc assumptions against live Arc Testnet before doing anything else. This is read-only, needs no credentials, and never signs or submits:

```bash
uv run arc-payables-verify-arc
```

It verifies the chain ID is `5042002`, that the documented USDC address has bytecode with `decimals() == 6` and `symbol() == USDC`, and, once configured, that the deployed guard's `policySigner()` and `paymentToken()` match your configuration and reports the Circle wallet's native and ERC-20 balances. It exits non-zero with an explicit `TODO` list while live payment is unconfigured.

1. In Circle Console, generate and register a **test** entity secret yourself; store the entity secret and recovery material outside this repo. Create a **Developer-Controlled SCA** on `ARC-TESTNET`; record its wallet ID and address. The Arc testnet faucet is at [faucet.circle.com](https://faucet.circle.com/).
2. Configure the local environment with `PAYMENT_PROVIDER=circle`, a test Circle API key/entity secret, wallet ID/address, `CIRCLE_GUARD_ADDRESS`, `PERMIT_SIGNING_PRIVATE_KEY`, and `CIRCLE_RPC_URL=https://rpc.testnet.arc.io`. `PERMIT_SIGNING_PRIVATE_KEY` is the policy signer corresponding to the guard's `policySigner`; it is separate from Circle's wallet credential. Do not place either secret in model context.
3. Deploy the guard using Foundry only to Arc Testnet. The deployer key is local-only; the constructor token is fixed to the documented Arc Testnet USDC address. With the local environment populated, run `forge script script/DeployPaymentGuard.s.sol:DeployPaymentGuard --rpc-url https://rpc.testnet.arc.io --broadcast` (the script refuses a non-Arc-Testnet chain ID). Save the deployed address in local config.
4. Set `API_KEY` and a separate `APPROVAL_TOKEN`. Test wallet balance, guard signer/token, and Circle's wallet chain/account type. Make a low-value sandbox payment only after the deterministic checks pass; Circle `COMPLETE` is required before ERPNext writeback.
5. Set `ACCOUNTING_PROVIDER=frappe` only after the sandbox mapping below is verified. Live writeback is otherwise disabled.

The backend refuses RPC chain IDs other than `5042002`. Never use this path with mainnet. Circle wallet/entity secrets and the policy signing key are read only by server adapters and never logged or serialized into the agent decision.

## Frappe / ERPNext adapter

The adapter uses Frappe REST API v1 (`/api/resource/...`) and the supported whitelisted submit method (`POST /api/method/frappe.client.submit`). It reads Supplier, Purchase Invoice, linked Purchase Order, and Purchase Receipt. The trusted wallet comes only from configured Supplier custom fields (`FRAPPE_SUPPLIER_WALLET_FIELD` and `FRAPPE_SUPPLIER_WALLET_VERIFIED_FIELD`); invoice payee fields remain untrusted.

Payable state is derived from the authoritative `docstatus` (with `status` as a label), never assumed: a missing or unparseable `docstatus` yields `UNKNOWN` and blocks payment, and an invoice with no linked Purchase Invoice yields `UNVERIFIED`. The connector also compares the captured invoice's amount, currency, supplier reference, linked invoice name, and line items against the ERP record; any difference is a non-overridable block.

Use a dedicated API user with least-privilege read plus Payment Entry and Journal Entry create/submit permissions; Administrator credentials are rejected. Before writing, the adapter verifies the configured account company/currency and invoice currency. **USDC is not silently treated as USD.** A live writeback requires explicit company, paid-from/paid-to/fee accounts, a cost centre, account and invoice currencies, source/target/invoice/fee exchange rates and conversion units. A missing Circle ERC-20 USDC fee amount is different: the provider often cannot report the fee while the transfer is still being indexed, so the writeback defers with a backoff, asks the provider again on the next attempt, and books the fee as soon as it can be named. Only the configuration above disables a writeback outright, because retrying a configuration problem cannot fix it.

A Payment Entry references the Purchase Invoice and stores `ARC-TESTNET:<tx hash>` in `reference_no`. The network-fee Journal Entry uses `ARC-TESTNET-FEE:<tx hash>`. Retries look up those references and verify party, invoice, accounts, and amounts before submitting or reusing a document, so a lost response cannot book anything twice. A custom field is not assumed.

The fee is **not** a Payment Entry deduction. ERPNext subtracts a deduction from what the party receives and books any gap between our outflow and the party amount as exchange gain or loss, so a fee we absorb has no place there. The Payment Entry is exactly the supplier's amount and balances on its own; the fee is a separate Journal Entry (debit the fee account, credit the settlement account). A company whose ledger cannot represent the measured fee books it rounded **up** to the company currency's smallest unit, and both figures are written to the entry's remark.

A sandbox administrator/accountant must confirm the actual chart-of-accounts mapping before enabling this connector. The **read/import path, the operator bootstrap and the full writeback have all been verified against a live ERPNext v15 sandbox**; `deploy/erpnext/README.md` records the documents and ledger lines that run produced.

## Decision layers

The advisory layer is optional and selectable; the deterministic policy is always the authority.

| `DECISION_LAYER` | Behaviour |
| --- | --- |
| `policy` | No advisory opinion at all. The deterministic checks are the only view, and the audit record says so. |
| `heuristics` (default) | The fast, explainable layer: named observations about evidence gaps and trade-offs, each with a reason and a confidence. No model call. |
| `dual_process` | The fast layer first; a bounded planner is consulted only for genuine trade-offs. |

What the planner may and may not do:

* It is asked only about trade-offs: the amount against the automatic limit, the treasury reserve, payment timing. A missing fact is never sent to it, because reasoning cannot supply evidence that does not exist.
* It may choose only from the actions the caller permits, and its reply is validated strictly. Anything malformed, slow, unavailable or off-list is rejected, and the fast layer's answer stands unchanged.
* It cannot change the destination, the amount, or whether approval is required. A planner that says `PAY_NOW` where the policy disagrees changes nothing: the disagreement is recorded and surfaces as an escalation.
* Its answers are recorded in the audit chain with model identity, prompt hash, response hash, latency and outcome, so "which layer decided, and on what basis" stays answerable long afterwards.

Captured free text (for example OCR output) reaches the planner only in an explicitly-labelled, bounded field, and the system prompt instructs the model to treat all evidence as data. An adversarial test drives a model that has "complied" with an injected instruction and asserts that nothing about the payment changes: not the destination, not the amount, not the authorization.

## Which payable to pay first

Individual evaluation answers "may this invoice be paid?". `GET /plan` answers the question a treasury actually faces: several invoices are payable, the balance covers some of them, and paying the wrong one first costs money.

The split between advice and money is deliberate:

* **Ordering is advisory.** The heuristic order is expiring discount first, then lateness, then imminent due dates, then the smaller obligation. The sort key is a tuple of business facts, not weights, so each position has a readable reason. With `DECISION_LAYER=dual_process` a planner may reorder the queue.
* **Spending is deterministic.** The allocation applies the reserve floor and the balance in code, by walking the order and stopping when the next invoice would breach the reserve. A planner may reorder; it may never decide how much leaves.

So the worst a confused planner can do is sequence the same payments differently. Validation requires exactly the offered invoices, each once, each with a reason: an added, omitted, duplicated or unexplained invoice rejects the whole answer and the deterministic order stands.

Building a plan is read-only: it derives its decisions from live evidence without recording them. Every invoice it ranks must still pass its own policy checks and settle through the same guarded payment path. Invoices that are not payable appear in the plan with the reason, so the queue stays visible in full instead of being filtered away.

## Treasury visibility and counterparty monitoring

`GET /forecast` answers the question a treasurer asks before either of the above: what is due, when, and where the balance stops covering it. Obligations are walked in **due-date order** against the balance, keeping the reserve intact, so the forecast and the payment plan cannot disagree about what is affordable. Two things are kept apart. An invoice whose evidence is incomplete is still money owed, so it is counted and flagged as *not payable by the agent*, with the reason. And the shortfall date is the first obligation the balance cannot cover, not the first invoice that happens to be urgent.

`arc-payables-rescreen` (or `POST /monitoring/rescreen`) re-screens every counterparty with an open invoice and keeps a **history**, so a risk-profile change is a transition rather than a silently overwritten status. A change is written to the audit chain of every open invoice it affects, which means the payment record shows when the counterparty's risk moved under it.

Screening scales authority; it never grants it:

| `SCREENING_MEDIUM_TIER_HANDLING` | Unclear screening (inconclusive / unavailable) | Flagged |
| --- | --- | --- |
| `review` (default) | a human must acknowledge it | a human must acknowledge it, and blocked by default |
| `limit` | payable unattended up to **25%** of the automatic limit | still requires a human |

`GET /suppliers` reports each counterparty's latest screening, tier, resulting automatic limit and open exposure. The tier is derived once and shared: the advisory layer and the authoritative policy read the same limit, so they cannot disagree about whether an amount is automatic.

## Workflow states and safety behavior

`RECEIVED → EVIDENCE_CHECKING → ELIGIBLE | WAITING | HELD | ESCALATED → AUTHORIZED → SUBMITTED → CONFIRMED → ERP_PENDING | ERP_RECORDED`.

`FAILED` and `NEEDS_RECONCILIATION` are terminal/manual-resolution paths. Database unique constraints serialize duplicate invoice/payment creation. A timeout after a transaction submission never starts a second payment blindly. ERP writeback retry is a separate `/invoices/{id}/payment/erp-writeback` request and cannot resubmit funds; while a writeback lease is held it returns `409 erp_writeback_in_progress` rather than reporting success. Human review is invoice-scoped, requires an authenticated reviewer token and explicit acknowledgement of each exception, and does not permit choosing a new destination address. Even after approval, the permit recipient is copied only from the Supplier record.

The authorization is bound to the evidence hash of the evaluated snapshot, which includes the treasury balance. If any material evidence changes between evaluation and payment (another payment moves the balance, the supplier record is edited, the ERP invoice amount is restated), payment returns `409 evidence_changed_after_evaluation`, records the fresh decision, and requires a new evaluation. A reviewer approval is likewise invalidated when the evidence it acknowledged changes. In the demo this means each invoice is an evaluate-then-pay cycle.

## Security and integration limitations

**Verified against live systems:** the whole path, on Arc Testnet and against a live ERPNext.

*Arc Testnet, real funds.* `PaymentGuard` at
`0xbe0477081f90e68b6699a585d5d31ad93d96f318` (deploy tx `0xb0e3b5fbc6a41db4dd6243b7454fe9dde31d305674f9e641f8f43d0d005139fe`, block 64882475) carries immutable budgets of 0.05 USDC per payment, 0.20 USDC per epoch and 0.05 USDC per recipient per epoch. An earlier guard at `0x79f4735b5cfb604bb7e3039b48e0e3f41892866b` held 2/5/2 USDC caps and settled three payments (2 USDC, 1 USDC, 0.01 USDC). Each recipient's ERC-20 balance rose by exactly the authorized amount, `arc-payables-verify-arc` read the deployed caps back off the chain and confirmed the chain id, the USDC bytecode, `decimals() == 6`, `symbol() == USDC`, the guard's policy signer and its payment token, and a 250 USDC invoice was refused by the preflight before anything was signed because the old contract's per-payment cap was 2 USDC.

*ERPNext.* Both payments were written back as a Payment Entry for exactly the supplier's amount plus
a network-fee Journal Entry, and the invoice reached `ERP_RECORDED`. A worker pass then paid a nano invoice unattended: tx `0x801b2ad9eb90b08e916fa65dea94d2dba70187532f1cc76d102876cccd53333b` sent 0.01 USDC through the nano-cap guard, and the sandbox holds `ACC-PAY-2026-00011` plus fee entry `ACC-JV-2026-00005` behind it. The bootstrap, the balanced GL
entries and an idempotent re-run were verified in the same sandbox.

*Circle Developer-Controlled Wallets.* A Circle SCA wallet on Arc Testnet settled 0.01 USDC through
the same guard: tx `0x007678e3a08575390a39b8a77ac7598847db3ca7f0fb8a01eae39a1b61741db0` (Circle operation
`f419795e-6677-5131-85e9-f7db1f429807`), and the recipient's balance rose by exactly the authorized
amount. The ledger took it as `ACC-PAY-2026-00012` with zero deductions plus fee entry
`ACC-JV-2026-00006`, and the invoice reached `ERP_RECORDED`. `arc-payables-verify-arc` reads the
wallet back from Circle and confirms it is the configured `ARC-TESTNET` SCA account before anything
is signed. On testnet Circle sponsors gas for the user operation, so the wallet is debited the
payment amount and nothing else.

The live runs also found four defects that no mock had: the native-gas to ERC-20 fee conversion was
off by a factor of 10\*\*6 (the first payment reported 1 micro-USDC where the chain says 3489, and the
corrected code then reported 3130 for the second against the chain's 3130), a misnamed trade limit
was silently ignored because settings accepted unknown fields, the deployment script used a
cheatcode this forge build does not have so it had never actually run, and the Circle adapter
accepted only a `networkFeeUsdc` object where Circle returns a plain 18-decimal `networkFee` scalar,
which left the first Circle settlement unbookable.

**Not verified live:**

- The **Circle Developer-Controlled Wallet** path has now settled real testnet USDC and reached
  `ERP_RECORDED`, but only on Arc Testnet. Circle's own `transactionScreeningEvaluation` returns no
  verdict, so address screening stays unavailable and invoices still escalate for a human. The fee
  recorded is Circle's reported cost of the operation that settled the payment; the exact-allowance
  approval before it is a separate Circle operation whose fee is not booked, exactly as on the
  local-key path. There is no mainnet route for either provider.
- The fee we absorb is booked rounded up to the company currency's smallest unit, because a ledger in USD cannot represent a fraction of a cent. The entry's remark records the measured figure, and the rounding can overstate our own cost by less than one unit per payment. A company whose base currency is the stablecoin needs no rounding.
- The live writeback above was verified on a **disposable local sandbox**. A production chart of accounts, a least-privilege runtime user, and an accountant's review of the mapping are still the operator's job.
- Address screening's OpenSanctions provider is implemented behind its interface with deterministic evidence and fail-closed behavior, but no live call has been made (no API key configured), so a real deployment still relies on human review until it is exercised.
- The fee is measured from the transaction receipt's `gasUsed * effectiveGasPrice`, which Arc reports as 18-decimal native USDC against a 6-decimal token. That conversion is now pinned by a test using a real receipt's numbers. A fee the ledger cannot represent is booked rounded up, and a company whose base currency is the settlement asset needs no rounding at all.

**Not built, on purpose:** the console is a static page over the documented API instead of a product UI, there is no mainnet route, only USDC settlement (currency support is extensible but unimplemented), and there is no KMS signer implementation: `SIGNER_BACKEND=kms` fails closed instead of pretending to sign.

**Operational prerequisites left to the operator:** replace and human-verify the demo supplier wallet (currently an unverified placeholder), create a narrowly scoped runtime ERPNext user, and decide address-screening vendor/keys.

- The sample policy limits, supplier, invoices, wallet, and mock tx hashes are demo data only.
- A new or unverified Supplier wallet cannot be supplied by an invoice. The only possible reviewed payment destination remains the address in the trusted Supplier record; updating that record is a separate privileged business process outside this agent.
