# Tameion — evidence-gated AP backend MVP

A backend-only AP workflow for reviewing supplier invoices and paying **USDC on Arc Testnet**. The invoice amount is evidence; the payment destination is always read from a trusted supplier record. A different invoice payee stops unattended payment and requires human review. The decision agent is a deterministic, read-only baseline; a separate deterministic policy, permit signer, payment provider, and approval gate control execution.

This repository began as the Arc Canteen Python/Foundry starter. There is no frontend.

## What is implemented

- FastAPI endpoints with generated OpenAPI at `/docs` and `/openapi.json`.
- SQLite migrations, explicit workflow states, append-only event history, invoice/payment uniqueness, request idempotency, and durable payment/ERP links.
- Evidence comparison for Supplier, Purchase Invoice, PO lines, Purchase Receipt quantities, duplicates, invoice payee, address screening, due dates/terms, early discounts, amount limits, and treasury reserve.
- `AccountingConnector`, `PaymentProvider`, `EvidenceStore`, `DecisionAgent`, and `CurrencyConverter` boundaries, with local mocks and vendor adapters.
- Circle Developer-Controlled Wallets adapter and a Foundry one-time payment guard. The real payment provider targets only `ARC-TESTNET` and verifies chain ID `5042002` before reads or writes.
- Frappe/ERPNext REST API v1 adapter. It creates/submits a standard Payment Entry only after Circle reports `COMPLETE`; ERP writeback retry never resubmits a blockchain payment.
- Mock seed data: an eligible supplier invoice and a suspicious invoice with an attacker payee, amount/PO mismatch, and insufficient receipt evidence, each backed by a separate simulated ERP record, plus an importable ERP invoice for the `POST /invoices/import` path.

## Arc/Circle path and security

Two executors implement the same payment path — an exact `approve`, then the policy-signed `PaymentGuard.pay` — so the flow can be exercised either way and the authorization rules do not depend on which one runs:

* **Circle Developer-Controlled Wallets** (`PAYMENT_PROVIDER=circle`) keeps the payer key at Circle. This is the production-shaped option.
* **Local key** (`PAYMENT_PROVIDER=local`) holds the payer key in the environment. It is Arc Testnet only, asserted from the node's `eth_chainId`, and exists so the path can be run and developed without Circle credentials. The guard's budget caps and exact-allowance behavior are identical.

Both refuse to proceed on an unconfigured or incomplete setup, and neither can exceed the guard's on-chain budget.

Arc Canteen context and current official docs were checked before choosing the adapter:

- Arc Testnet chain ID: `5042002`; RPC: `https://rpc.testnet.arc.io`; testnet-only deployment.
- Arc's USDC ERC-20 interface: `0x3600000000000000000000000000000000000000`, 6 decimals. Arc also uses native USDC for gas at 18-decimal precision; this code uses ERC-20 6-decimal units for payment amounts and checks the native balance separately for gas.
- Circle Developer-Controlled Wallets list Arc Testnet and SCA support. The bundled Circle Wallets OpenAPI includes `ARC-TESTNET` for the contract-execution endpoint. The adapter uses that documented contract-call path, not a raw transfer or a wallet private key. It requires a Circle SCA whose wallet ID/address and chain are verified.
- `PaymentGuard` verifies an EIP-712 policy signature bound to payer, token, trusted recipient, exact amount, evidence hash, unique payment ID, expiry, chain ID, and guard contract domain. Its replay mapping consumes the unique payment ID before the exact `transferFrom`.
- Circle executes an exact ERC-20 allowance (zero-reset if needed) before the guard call; no unlimited approval. Each Circle operation has a durable UUID v4 idempotency key. An uncertain operation is inspected using Circle status and the on-chain payment ID before any retry. If it cannot be resolved, the invoice remains in `NEEDS_RECONCILIATION`.
- The model receives no signing credential or payment tool. The MVP contains no LLM adapter; `DecisionAgent` is deterministic and has no `PaymentProvider` reference. Invoice/OCR text is untrusted, hashed, and not retained as raw text.

Sources used: [Arc RPC endpoints](https://docs.arc.io/arc/references/rpc-endpoints), [Arc contract addresses](https://docs.arc.io/arc/references/contract-addresses), [Circle Wallets supported blockchains](https://developers.circle.com/wallets/supported-blockchains), [Circle dev-controlled transfers](https://developers.circle.com/wallets/dev-controlled/transfer-tokens-across-wallets), [Circle entity-secret sample](https://github.com/circlefin/w3s-entity-secret-sample-code), [Frappe REST API](https://docs.frappe.io/framework/user/en/api/rest), [ERPNext Payment Entry](https://docs.frappe.io/erpnext/payment-entry).

## Local setup and demo

Requirements: Python 3.11+, `uv`, Foundry (`forge`).

```bash
uv sync
cp .env.example .env     # optional for defaults; never commit this local file
uv run tameion-migrate
uv run tameion-seed      # seeds mock records
uv run uvicorn tameion.api:app --reload
uv run tameion-verify-arc  # optional read-only check against live Arc Testnet
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

API routes are listed in Swagger at `/docs`. Configure `API_KEY` before enabling Circle or Frappe adapters; write endpoints reject external-adapter operation without API auth. Human approval also requires a separate `APPROVAL_TOKEN`, and a reviewer may only acknowledge the listed exceptions — never choose a destination address. Do not put secrets in chat, command history, source control, logs, or model context.

## Sending a real Arc Testnet payment

`tameion-live-run` sends one payment, and refuses to guess. It checks the chain id from the
node, the deployed guard's budgets, that the destination is a **human-verified** trusted
Supplier wallet, that the invoice is linked to an acceptable accounting payable, and that the
treasury covers the amount while preserving the reserve. Nothing is signed without `--confirm`,
so the dry run is free:

```bash
uv run tameion-verify-arc                        # read-only: is this deployment ready, and what is missing?
uv run tameion-live-run --invoice <invoice-id>   # preflight only
uv run tameion-live-run --invoice <invoice-id> --confirm
```

Setup, in order — the first three need your keys, which never leave your machine:

```bash
arc-canteen login                                # a funded Arc Testnet wallet and an RPC URL
export RPC=$(arc-canteen rpc-url)
cast wallet new                                  # the supplier destination you control
cast wallet new                                  # the policy signer; must differ from the deployer

export DEPLOYER_PRIVATE_KEY=<arc-canteen wallet key>
export PERMIT_SIGNING_PRIVATE_KEY=<policy signer key>
export PAYMENT_GUARD_PER_PAYMENT_CAP=20000000    # 20 USDC per payment, in 6-decimal units
export PAYMENT_GUARD_EPOCH_CAP=100000000
export PAYMENT_GUARD_RECIPIENT_EPOCH_CAP=50000000
export PAYMENT_GUARD_EPOCH_LENGTH_SECONDS=86400
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

Used today: **Developer-Controlled Wallets** (the production-shaped payer, in `circle_adapter.py`),
**USDC** on Arc, and **Contracts** in the sense that the budget is enforced by an on-chain guard
rather than by configuration.

Not yet used, each of which needs a Circle account and credentials that only the operator can
create: **Paymaster** (sponsoring gas instead of the payer holding it), **App Kit** (Send,
Unified Balance), **CCTP** and **Gateway** (moving USDC between chains as one balance), **USYC**
(a yield-bearing reserve) and **EURC** (paying a euro-denominated vendor). The ports already
exist for the payment and accounting sides, so each is an adapter plus tests rather than a
redesign.

## Database and tests

Migrations are in `migrations/`; the service applies them on startup. The DB defaults to `data/tameion.sqlite3` (`DATABASE_PATH` overrides it).

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
uv run tameion-verify-arc
```

It verifies the chain ID is `5042002`, that the documented USDC address has bytecode with `decimals() == 6` and `symbol() == USDC`, and — once configured — that the deployed guard's `policySigner()` and `paymentToken()` match your configuration and reports the Circle wallet's native and ERC-20 balances. It exits non-zero with an explicit `TODO` list while live payment is unconfigured.

1. In Circle Console, generate and register a **test** entity secret yourself; store the entity secret and recovery material outside this repo. Create a **Developer-Controlled SCA** on `ARC-TESTNET`; record its wallet ID and address. The Arc testnet faucet is at [faucet.circle.com](https://faucet.circle.com/).
2. Configure the local environment with `PAYMENT_PROVIDER=circle`, a test Circle API key/entity secret, wallet ID/address, `CIRCLE_GUARD_ADDRESS`, `PERMIT_SIGNING_PRIVATE_KEY`, and `CIRCLE_RPC_URL=https://rpc.testnet.arc.io`. `PERMIT_SIGNING_PRIVATE_KEY` is the policy signer corresponding to the guard's `policySigner`; it is separate from Circle's wallet credential. Do not place either secret in model context.
3. Deploy the guard using Foundry only to Arc Testnet. The deployer key is local-only; the constructor token is fixed to the documented Arc Testnet USDC address. With the local environment populated, run `forge script script/DeployPaymentGuard.s.sol:DeployPaymentGuard --rpc-url https://rpc.testnet.arc.io --broadcast` (the script refuses a non-Arc-Testnet chain ID). Save the deployed address in local config.
4. Set `API_KEY` and a separate `APPROVAL_TOKEN`. Test wallet balance, guard signer/token, and Circle's wallet chain/account type. Make a low-value sandbox payment only after the deterministic checks pass; Circle `COMPLETE` is required before ERPNext writeback.
5. Set `ACCOUNTING_PROVIDER=frappe` only after the sandbox mapping below is verified. Live writeback is otherwise disabled.

The backend refuses RPC chain IDs other than `5042002`. Never use this path with mainnet. Circle wallet/entity secrets and the policy signing key are read only by server adapters and never logged or serialized into the agent decision.

## Frappe / ERPNext adapter

The adapter uses Frappe REST API v1 (`/api/resource/...`) and the supported whitelisted submit method (`POST /api/method/frappe.client.submit`). It reads Supplier, Purchase Invoice, linked Purchase Order, and Purchase Receipt. The trusted wallet comes only from configured Supplier custom fields (`FRAPPE_SUPPLIER_WALLET_FIELD` and `FRAPPE_SUPPLIER_WALLET_VERIFIED_FIELD`); invoice payee fields remain untrusted.

Payable state is derived from the authoritative `docstatus` (with `status` as a label), never assumed: a missing or unparseable `docstatus` yields `UNKNOWN` and blocks payment, and an invoice with no linked Purchase Invoice yields `UNVERIFIED`. The connector also compares the captured invoice's amount, currency, supplier reference, linked invoice name, and line items against the ERP record; any difference is a non-overridable block.

Use a dedicated API user with least-privilege read/Payment Entry create+submit permissions; Administrator credentials are rejected. Before writing, the adapter verifies the configured account company/currency and invoice currency. **USDC is not silently treated as USD.** A live Payment Entry requires explicit company, paid-from/paid-to/fee accounts, account and invoice currencies, source/target/invoice/fee exchange rates and conversion units, and a known Circle ERC-20 USDC fee amount. If any are missing, live writeback remains disabled. The standard Payment Entry references the Purchase Invoice and stores `ARC-TESTNET:<tx hash>` in `reference_no`; retries look up that reference and verify party, invoice, and amounts before submitting/reusing a draft. A custom field is not assumed.

A sandbox administrator/accountant must confirm the actual chart-of-accounts mapping, invoice currency, exchange-rate direction, fee deduction account and exact network-fee field returned by Circle before enabling this connector. The **read/import path and the operator bootstrap have been verified against a live ERPNext v15 sandbox** (see `deploy/erpnext/README.md`); the Payment Entry **writeback has not**: it is covered by mock tests only, so the first live writeback needs supervised verification.

## Decision layers

The advisory layer is optional and selectable; the deterministic policy is always the authority.

| `DECISION_LAYER` | Behaviour |
| --- | --- |
| `policy` | No advisory opinion at all. The deterministic checks are the only view, and the audit record says so. |
| `heuristics` (default) | The fast, explainable layer: named observations about evidence gaps and trade-offs, each with a reason and a confidence. No model call. |
| `dual_process` | The fast layer first; a bounded planner is consulted only for genuine trade-offs. |

What the planner may and may not do:

* It is asked only about trade-offs — amount against the automatic limit, treasury reserve, payment timing. A missing fact is never sent to it, because reasoning cannot supply evidence that does not exist.
* It may choose only from the actions the caller permits, and its reply is validated strictly. Anything malformed, slow, unavailable or off-list is rejected, and the fast layer's answer stands unchanged.
* It cannot change the destination, the amount, or whether approval is required. A planner that says `PAY_NOW` where the policy disagrees changes nothing: the disagreement is recorded and surfaces as an escalation.
* Its answers are recorded in the audit chain with model identity, prompt hash, response hash, latency and outcome, so "which layer decided, and on what basis" stays answerable long afterwards.

Captured free text (for example OCR output) reaches the planner only in an explicitly-labelled, bounded field, and the system prompt instructs the model to treat all evidence as data. An adversarial test drives a model that has "complied" with an injected instruction and asserts that nothing about the payment — destination, amount, authorization — changes.

## Which payable to pay first

Individual evaluation answers "may this invoice be paid?". `GET /plan` answers the question a treasury actually faces: several invoices are payable, the balance covers some of them, and paying the wrong one first costs money.

The split between advice and money is deliberate:

* **Ordering is advisory.** The heuristic order is expiring discount first, then lateness, then imminent due dates, then the smaller obligation — a tuple of business facts rather than weights, so each position has a readable reason. With `DECISION_LAYER=dual_process` a planner may reorder the queue.
* **Spending is deterministic.** The allocation applies the reserve floor and the balance in code, by walking the order and stopping when the next invoice would breach the reserve. A planner may reorder; it may never decide how much leaves.

So the worst a confused planner can do is sequence the same payments differently. Validation requires exactly the offered invoices, each once, each with a reason: an added, omitted, duplicated or unexplained invoice rejects the whole answer and the deterministic order stands.

Building a plan is read-only — it derives decisions from live evidence without recording them — and every invoice it ranks must still pass its own policy checks and settle through the same guarded payment path. Invoices that are not payable appear in the plan with the reason, so the queue stays visible in full rather than silently filtered.

## Workflow states and safety behavior

`RECEIVED → EVIDENCE_CHECKING → ELIGIBLE | WAITING | HELD | ESCALATED → AUTHORIZED → SUBMITTED → CONFIRMED → ERP_PENDING | ERP_RECORDED`.

`FAILED` and `NEEDS_RECONCILIATION` are terminal/manual-resolution paths. Database unique constraints serialize duplicate invoice/payment creation. A timeout after a transaction submission never starts a second payment blindly. ERP writeback retry is a separate `/invoices/{id}/payment/erp-writeback` request and cannot resubmit funds; while a writeback lease is held it returns `409 erp_writeback_in_progress` rather than reporting success. Human review is invoice-scoped, requires an authenticated reviewer token and explicit acknowledgement of each exception, and does not permit choosing a new destination address. Even after approval, the permit recipient is copied only from the Supplier record.

The authorization is bound to the evidence hash of the evaluated snapshot, which includes the treasury balance. If any material evidence changes between evaluation and payment — another payment moves the balance, the supplier record is edited, the ERP invoice amount is restated — payment returns `409 evidence_changed_after_evaluation`, records the fresh decision, and requires a new evaluation. A reviewer approval is likewise invalidated when the evidence it acknowledged changes. In the demo this means each invoice is an evaluate-then-pay cycle.

## Security and integration limitations

**Verified against live systems:** the ERPNext read/import path and the operator bootstrap (company `Arc Demo Inc`, submitted PO/PR/PI, balanced GL entries, idempotent re-run), and Arc Testnet *read-only* facts (chain ID `5042002`, USDC bytecode, `decimals() == 6`, `symbol() == USDC`).

**Not verified live:**

- A real Arc testnet payment was **not** executed. Circle credentials, a funded Circle `ARC-TESTNET` SCA, and a deployed guard are not configured, and no real funds moved. The Circle path is exercised only against the credential-free emulator plus the real `PaymentGuard` bytecode.
- The Frappe **Payment Entry writeback** has not been run against the live sandbox; it is covered by mock tests only. The first live write must be supervised.
- Address screening's OpenSanctions provider is implemented behind its interface with deterministic evidence and fail-closed behavior, but no live call has been made (no API key configured), so a real deployment still relies on human review until it is exercised.
- The Circle response's exact network-fee representation must be verified live. Live writeback accepts only an explicitly identified ERC-20 USDC fee with 6-decimal precision; a scalar fee or Arc native-USDC fee (18 decimals) is treated as unknown, so the confirmed payment remains `ERP_PENDING` and cannot be written to ERPNext.

**Deferred by design, not by accident:** no frontend (backend first), no mainnet route, only USDC settlement (currency support is extensible but unimplemented), and no KMS signer implementation — `SIGNER_BACKEND=kms` fails closed rather than pretending to sign.

**Operational prerequisites left to the operator:** replace and human-verify the demo supplier wallet (currently an unverified placeholder), create a narrowly scoped runtime ERPNext user, and decide address-screening vendor/keys.

- The sample policy limits, supplier, invoices, wallet, and mock tx hashes are demo data only.
- A new or unverified Supplier wallet cannot be supplied by an invoice. The only possible reviewed payment destination remains the address in the trusted Supplier record; updating that record is a separate privileged business process outside this agent.
