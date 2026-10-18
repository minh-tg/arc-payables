# Demo runbook

This is the shot list and optional Arc Testnet integration runbook. Start with the
[manual readiness checks](docs/manual-readiness.md) before recording or recruiting a pilot.
That walkthrough is mock-only. The live commands below require separate operator authorization;
testnet execution moves test funds and is not the same as simulation.

For an existing Circle-backed settlement, use the [saved evidence packet](docs/submission-evidence.md).
The local-key example below is an alternative executor, not proof of Circle usage.

The point of the demo is one sentence: an agent proposes a payment, and something the agent cannot
reach decides whether it happens.

## Before you start

Five minutes, and only the first step needs your keys.

```bash
arc-canteen login                 # funded Arc Testnet wallet plus an RPC URL
export RPC=$(arc-canteen rpc-url)
cast wallet new                   # the supplier destination: an address you control
cast wallet new                   # the policy signer, which must differ from the deployer
```

Deploy the guard with an explicit budget. Amounts are ERC-20 USDC units at six decimals.

```bash
export DEPLOYER_PRIVATE_KEY=<arc-canteen wallet key>
export PERMIT_SIGNING_PRIVATE_KEY=<policy signer key>
export PAYMENT_GUARD_PER_PAYMENT_CAP=50000          # 0.05 USDC per payment
export PAYMENT_GUARD_EPOCH_CAP=200000               # 0.20 USDC per epoch
export PAYMENT_GUARD_RECIPIENT_EPOCH_CAP=50000      # 0.05 USDC per supplier per epoch
export PAYMENT_GUARD_EPOCH_LENGTH_SECONDS=86400
export PAYMENT_GUARD_PAUSER=<an operator address that may pause payments>
forge script script/DeployPaymentGuard.s.sol --rpc-url $RPC --broadcast
```

Point the app at it:

```bash
export PAYMENT_PROVIDER=local
export LOCAL_PAYMENT_PRIVATE_KEY=<treasury payer key>
export LOCAL_PAYMENT_GUARD_ADDRESS=<guard address from the deploy>
export LOCAL_PAYMENT_RPC_URL=$RPC
# A real provider with shared credentials needs the explicit testnet-token mode. With the default
# AUTH_MODE=demo the API refuses every call with 503 api_auth_not_configured, because demo
# credentials must never open an external-provider deployment. This is a testnet path, not identity.
export AUTH_MODE=testnet_tokens
export API_KEY=local-demo-key
export APPROVAL_TOKEN=local-demo-token
```

## Choose the amounts

**Pay tiny invoices.** A $5 testnet wallet is not there to be spent: it is there to prove the path.
Every step below costs gas, and the amounts are yours to choose, so the cheapest honest demo uses
amounts of a cent or less. The caps above are 0.05 USDC for that reason.

What each step actually costs on Arc Testnet, measured from real receipts:

| Step | Cost |
| --- | --- |
| deploying the guard | ~0.038 USDC |
| the exact-allowance approval before a payment | ~0.0012 USDC |
| the payment itself | ~0.0031 USDC |
| a plain USDC transfer | ~0.0012 USDC |

So a 0.01 USDC invoice costs about 0.014 USDC end to end, and the gas is 31% of the amount paid.
That ratio is the point of paying small: at AP scale the fee is noise, and at nano scale it is the
loudest number on the receipt.

The demo policy's own defaults are a 2,000 USDC reserve floor and a 1,000 USDC automatic limit.
Those are examples, not recommendations, and they must be lowered for a small wallet: the balance
has to cover the invoice plus the reserve.

```bash
export MIN_RESERVE_USDC=0.10
export MAX_INVOICE_USDC=0.01
uv run arc-payables-erpnext-bootstrap --company 'Arc Demo Inc' \
  --wallet "$DEMO_SUPPLIER_WALLET" --quantity 1 --rate 0.01 \
  --invoice-reference DEMO-NANO-001 --apply
```

Two things to expect at that size. The guard's caps are maxima, so a small payment passes a large
cap; the caps only have to cover the amount. And ERPNext books a fee in the company currency, where
the smallest representable amount is 0.01 USD, so a measured 0.003 USDC fee is booked rounded up to
0.01 and the fee line can equal or exceed a nano payment. That is a property of a USD ledger, not of
the payment: the supplier still receives exactly the invoiced amount.

Pay a larger invoice only if you fund the wallet from
[testmint.myproceeds.xyz](https://testmint.myproceeds.xyz/), and then the guard caps must cover the
amount as well, because they are fixed when the guard is deployed.

## Or let it run itself

```bash
uv run arc-payables-worker --interval 30 --autopay
```

That is the same path as above with nobody pressing anything: it reconciles settlements, finishes
ledger writebacks, re-screens counterparties, and pays only the invoices the policy already marked
payable. It cannot approve an escalated invoice, and the guard's caps apply to it exactly as they
apply to you. `GET /metrics` is the scoreboard for it.

## The flow

### 1. A payable exists

Import the ERPNext Purchase Invoice. Any other route into the system holds the invoice at `HELD`
until a human links it to an accounting payable, because a captured document is not a payable.

```bash
KEY=$(uv run python -c 'import uuid; print(uuid.uuid4())')   # the API requires a UUID v4
curl -s -X POST localhost:8000/invoices/import -H "X-API-Key: $API_KEY" \
  -H "Idempotency-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"external_invoice_id":"ACC-PINV-2026-00007"}'
```

### 2. It is evaluated

```bash
curl -s -X POST localhost:8000/invoices/<invoice-id>/evaluate -H "X-API-Key: $API_KEY"
```

The response is a decision with the checks behind it. Look at `policy_checks`: each one names the
evidence it used. Look at `advisory.decided_by` to see which layer reached the conclusion.

### 3. A human verifies the wallet, once

In ERPNext, open the supplier record, set the wallet address to the one from `cast wallet new`, and
tick `USDC Wallet Verified`. This is the only way a payment destination changes, and payments are
refused until it happens.

### 4. It is paid

```bash
uv run arc-payables-live-run --invoice <invoice-id>             # preflight, sends nothing
uv run arc-payables-live-run --invoice <invoice-id> --confirm   # sends
```

The preflight re-reads the chain ID from the node, checks the guard's budgets against this specific
payment, confirms the destination is the verified supplier wallet, and refuses if the treasury would
drop below the reserve. It prints the transaction hash and an explorer link.

### 5. It lands in the books

Point the accounting side at the sandbox first, using the values the bootstrap printed. With the
mock accounting provider instead, this step is a local simulation and nothing leaves the database.
With `PAYMENT_PROVIDER=local` and a funded wallet this same flow has been run for real on Arc
Testnet, and with a live ERPNext mapping it has been run against a real instance: 2 USDC and 1 USDC
reached two recipients exactly, each with a Payment Entry and a fee Journal Entry behind it.

```bash
export ACCOUNTING_PROVIDER=frappe
export FRAPPE_URL=http://127.0.0.1:8080
export FRAPPE_API_KEY=<scoped API user>
export FRAPPE_API_SECRET=<scoped API secret>
# plus the FRAPPE_COMPANY, FRAPPE_PAID_FROM_ACCOUNT, FRAPPE_PAID_TO_ACCOUNT, FRAPPE_FEE_ACCOUNT,
# FRAPPE_COST_CENTER,
# FRAPPE_MODE_OF_PAYMENT and currency values printed by the bootstrap

curl -s -X POST localhost:8000/invoices/<invoice-id>/payment/erp-writeback -H "X-API-Key: $API_KEY"
```

In ERPNext, the Payment Entry references the transaction hash. The supplier leg is the authorized
amount exactly, and the network fee sits in its own expense account.

### 6. The record can be checked

```bash
curl -s localhost:8000/audit/verify -H "X-API-Key: $API_KEY"
```

It recomputes the hash chain over every event and reports the first entry that fails, if any. The
console at `/console` shows the same per invoice, with a verification button.

## Failure cases

Thirty seconds each, and they are the most convincing part of the demo because they show the system
refusing. All of these run locally with no funds.

```bash
uv run pytest tests/test_circle_executor.py -q -k budget          # the contract refuses a payment the policy approved
uv run pytest tests/test_workflow.py -q -k suspicious             # an invoice payee that differs from the supplier record
uv run pytest tests/test_monitoring.py -q -k reduced              # a risk change that cuts the unattended limit
uv run pytest tests/test_prioritisation.py -q                     # two invoices each affordable, only one jointly
```

## Stopping payments

If something is wrong, the guard can be paused instead of redeployed. The pauser address was set at
deployment and can only stop and resume payments, so this is not a way for anyone to take money, it
is a way to stop it:

```bash
cast send $GUARD "pause()" --private-key $PAUSER_KEY --rpc-url $RPC
uv run arc-payables-verify-arc      # reports the guard as paused, and therefore not ready
uv run arc-payables-live-run --invoice <invoice-id>   # refuses before signing anything
cast send $GUARD "unpause()" --private-key $PAUSER_KEY --rpc-url $RPC
```

## What not to claim

Repository tests are hermetic and do not submit payments to the public testnet. Existing live
Arc Testnet runs are documented; a recording is not necessarily the first payment. The
[evidence packet](docs/submission-evidence.md) links one 0.01 test-USDC Circle settlement to
ERPNext sandbox documents, while the original application audit/approval record, aggregate fee
attribution and economic sponsorship treatment remain unverified. Sandbox writeback is not
production accounting assurance. Fixture screening is simulated; unavailable screening requires
human review. No live vendor screening is evidenced here. Mainnet is unsupported and the guard
refuses deployment off Arc Testnet. Do not claim production readiness or customer adoption from
synthetic invoices and controlled wallets.
