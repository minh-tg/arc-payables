# Production settlement assurance

This document has two jobs, and they are deliberately kept apart:

1. **What the code does now**: `arc-payables-reconcile` compares every recorded payment with the
   guard's own `PaymentExecuted` log, and reports the disagreements it can prove.
2. **What engineering cannot assert**: the settlement path is still Arc Testnet only, and a
   production settlement deployment needs evidence this repository cannot produce — an independent
   review, a staged live validation, and a provider exercise. Those are listed as gates below.

Nothing here enables mainnet, weakens a cap, or removes a chain check. The guard still refuses any
chain that is not Arc Testnet, and this PR does not touch the contract.

## The principle: absence is not a finding

Every conclusion in a reconciliation run falls into one of two classes, and conflating them is the
failure mode this tool exists to avoid:

* **Observed.** The run saw an event. Statements about observed events are safe: this payment
  settled, for this amount, to this recipient, from this transaction. The scan range is irrelevant
  to them.
* **Absent.** The run did not see an event. What that means depends entirely on how much history was
  searched. A payment submitted before `--from-block` is simply not visible, so its absence proves
  nothing.

The two mistakes are not symmetric. Reporting a settled payment as failed can pay a supplier twice.
Reporting an unresolved one as settled hides money that left. So the tool never averages them into a
single "ok": it reports a blocked conclusion and an unresolved question separately, and the exit
code reflects only the first.

The one case where absence *is* proof is a search that starts at block 0. That is why the default
range is from genesis, and why a partial range changes the conclusion rather than the confidence:

| Search range | Record claims settled, no event found | Record attempted, no event found |
| --- | --- | --- |
| from block 0 | `settlement_without_evidence` — **blocking** | `not_settled_proven` — review |
| from a later block | `unresolved_uncertainty` — review | `unresolved_uncertainty` — review |

A recorded transaction hash is better evidence than any range: the receipt either contains the
guard's event for this payment or it does not, so that path needs no window at all.

## What is checked

| Finding | Severity | What it means | Operator action |
| --- | --- | --- | --- |
| `chain_unreachable` | blocking | The chain or its logs could not be read. **No settlement conclusion was drawn.** | Fix connectivity and re-run. Do not read this as "nothing settled". |
| `wrong_chain` | blocking | The endpoint is not the deployment's chain. | Point at the right endpoint. Never reconcile a testnet record against another chain. |
| `no_guard_configured` | blocking | There is no guard address, so there is nothing to compare against. | Configure the guard. A settlement cannot be confirmed without the contract that made it. |
| `settled_not_recorded` | blocking | The guard settled a payment with no row in the database. | Treat as an incident: money moved with no authorization record. Find out why before resuming. |
| `duplicate_settlement_events` | blocking | One payment id settled more than once. The contract should make this impossible. | Wrong guard, wrong endpoint, or worse. Stop and investigate. |
| `duplicate_payment_id_records` | blocking | One payment id recorded against two invoices. | One authorization cannot belong to two obligations; correct the records. |
| `shared_transaction_hash` | blocking | One transaction recorded against two payments. | The chain settled it once; one of the records is wrong. |
| `recorded_transaction_lacks_event` | blocking | The recorded transaction does not exist, or contains no settlement for this payment. | The recorded transaction is not the settlement. Resolve from the chain, do not resend. |
| `settlement_without_evidence` | blocking | Record claims settled; complete-coverage search finds nothing. | Ledger and chain disagree about whether money moved. Reconcile by hand before any further payment. |
| `settled_but_recorded_failed` | blocking | Record says FAILED; the guard settled it. | The books say no money moved and it did. Correct the ledger; do not pay again. |
| `amount_mismatch`, `recipient_mismatch`, `token_mismatch`, `evidence_mismatch`, `payer_mismatch` | blocking | The permit on file is not the permit that was paid. | The decision that was paid is not the decision on file. Treat as an incident. |
| `guard_mismatch`, `chain_id_mismatch` | blocking | The record belongs to a different deployment or chain. | Reconcile it against the deployment that authorized it. |
| `malformed_event` | blocking | A log from the guard address cannot be decoded. | Settlement history is not fully accounted for; inspect the raw logs. |
| `unresolved_uncertainty` | review | Absence within a partial range, or a receipt that could not be read. | Re-run with `--from-block 0`, or resolve from the recorded transaction, the guard's `used()` mapping, or the provider. **Never resend on this evidence alone.** |
| `provider_resolution_required` | review | A custodial submission is not visible on chain. | Only the provider can resolve its outcome. |
| `not_settled_proven` | review | The authorization did not settle, and the search covered the whole chain. | Re-evaluate the invoice; do not resend the old authorization. |
| `range_truncated` | review | The requested range exceeded the scan cap, so only the newest part was searched. | Caveat about coverage, not a funds fault. Narrow the range if you need the older history. |
| `provider_not_live` | review | The configured provider is the mock, which moves no money. | Nothing to reconcile. This report says nothing about a real chain. |

## Running it

Read-only: it signs nothing, sends nothing and repairs nothing. A tool that could also fix things
would be the wrong tool to point at an incident.

```sh
# Every recorded payment against the whole chain, exit 1 on any blocking finding
uv run arc-payables-reconcile

# JSON, for a scheduler or a dashboard
uv run arc-payables-reconcile --json

# A bounded window, when the chain is large (absence is then not proof)
uv run arc-payables-reconcile --from-block 64000000 --to-block 64100000
```

Exit codes: `0` no blocking findings, `1` at least one blocking finding, `2` the chain could not be
read at all.

Suggested cadence: after any unattended payment run, before closing the books for a period, and
after any incident. The report names the coverage it used, so a stored report can be audited later
for what it did and did not look at.

### On a blocking finding

1. Stop unattended spending (`WORKER_AUTOPAY=false` or stop the worker).
2. Preserve the database, the audit log and the raw logs. Do not edit records to make the report
   pass; the discrepancy is the finding.
3. Resolve it by hand against the chain. `settled_not_recorded` and `settled_but_recorded_failed`
   mean money moved that the books deny.
4. If a key is suspected, follow [docs/signing.md](signing.md) compromise recovery: rotate the guard,
   and do **not** mark the compromised address as retired.
5. Re-run reconciliation afterwards. `ok: true` is the evidence that the books and the chain agree
   again, not that the incident is closed.

### On an unresolved finding

Unresolved is not the same as failed. The financial remedy for a wrong "failed" here is paying twice.
Re-run with `--from-block 0` to convert uncertainty into a conclusion; if the range is too large to
scan, resolve the individual payment from its transaction, the guard's `used(paymentId)` mapping, or
the provider, and record the answer.

## Production settlement: the architecture this needs

The current executor and contract intentionally enforce Arc Testnet. A production settlement
deployment is a **separate deployment**, not a configuration flag here:

* Its own guard instance with its own immutable caps, deployed and verified on the target chain.
* Its own policy key on a real HSM, with the address pinned, per [docs/signing.md](signing.md).
* Its own provider credentials, least-privilege, and its own reconciliation cadence against that
  chain's own guard address.
* Its own database and audit history. Records from one deployment must never be reconciled against
  another's guard — `guard_mismatch` exists to catch exactly that.
* Chain identity bound explicitly rather than assumed, so a deployment cannot silently read the
  wrong chain.

Changing the chain check is not a production step. The guard refuses non-Arc chains by construction,
and weakening that without an independent review would remove the property that a compromised
backend cannot spend outside the reviewed deployment.

## Release gates that this repository cannot close

These are required before real funds. They are external by nature; a passing test suite is not a
substitute for any of them.

| Gate | Why it cannot be closed here | Evidence that closes it |
| --- | --- | --- |
| Independent security review | Requires a party who did not write the code. | A review of the guard, the signing path, the executor and the policy, with findings dispositioned. |
| Real-HSM exercise | SoftHSM2 is a software double; it proves the API path, not hardware isolation or key custody. | Generated-and-never-exportable keys on production hardware, attribute assertions, backup/restore drill. |
| Staged live validation | Testnet settlement is not production settlement. | A capped live run on the production deployment with reconciliation clean, starting below the smallest meaningful amount. |
| Settlement and reconciliation validation | Needs real provider behaviour, real latency and real failure modes. | Reconciliation clean over a period that includes at least one injected failure and one provider outage. |
| Provider exercise | Custodial outcomes and screening verdicts only appear live. | Documented exercise of the provider's resolution path, including a pending and an uncertain operation. |
| Operational ownership | Alerting must be delivered and acted on by a named person. | Alert delivery proven, on-call named, incident runbook walked through once. |
| Accounting validation | Needs a real chart of accounts and an accountant. | PR 5 of this series: accountant-approved mappings, complete fee booking, least-privilege ERP user. |

Until those are done, the accurate description of this project remains what the README says: an Arc
Testnet system, with Circle custody and a contract-bounded budget, that is not holding anyone's
production treasury.
