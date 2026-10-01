# Project history

## Where this started

Arc Payables began as an empty scaffold at the start of the event window: Canteen's Arc starter
workspace (`Counter.sol`, `foundry.toml`, `package.json`) and two commits. Everything else in this
repository was built during the window, and the boundary is recorded here plainly because the work
is judged on the delta rather than on a snapshot.

| | 27 September, event start | Now |
| --- | --- | --- |
| Commits | 2, scaffold only | every commit dated inside the window |
| Files | 13 | the scaffold plus a package, a contract, a service and a deployment |
| Tests | none | 420 Python and 26 Solidity, all hermetic |
| Contracts | the starter `Counter.sol` | `PaymentGuard` with immutable budgets, deployed to Arc Testnet and redeployed with nano caps |
| Integrations | none | Circle Developer-Controlled Wallets, ERPNext over REST, an advisory decision layer, and a credential-free executor that runs the real guard |
| Live settlements | none | six Arc Testnet payments, 3.04 USDC in total, five written back to ERPNext |
| Runs unattended | no | a worker pass that reconciles, finishes the ledger, discovers payables, evaluates them and pays what policy authorizes |

## What shipped during Tameion

Grouped by what it changed, not by commit order. The full log is `git log`.

**The contract.** `PaymentGuard` holds per-payment, per-epoch and per-recipient-per-epoch caps that
are immutable at deployment, consumed by payment id, with a separate pauser that cannot move funds.
19 unit tests and 7 invariants, plus an Arc-Testnet-only deploy script that refuses a stated budget
of zero.

**The workflow.** Evidence-gated accounts payable: read the payable from ERPNext, check what the
invoice claims against what the accounting system holds, decide with a deterministic policy, bind the
authorization to the evidence hash, settle through the guard, then write back a Payment Entry for
exactly the supplier's amount plus the network fee as its own Journal Entry. Idempotent by reference,
never retried blind, and it refuses before signing when anything does not line up.

**Discovery and the queue.** The payable queue, the payment plan, a forecast that walks obligations
in due-date order and now counts expected inflows too, risk-tiered limits from screening, continuous
re-screening on a cadence, and a reconciling worker loop with backoff, alerts and a pass history.

**Operations.** Prometheus metrics, an operator console with no build step, a supervised single-host
Compose stack with health checks, and a worker status view that reports the last pass and what it
tried to raise. The console's design system was prototyped in OpenDesign and then ported into the
application's own static files, and a headless-browser smoke script renders every view so a view that
parses but throws cannot ship unnoticed.

**Verification.** Three defects that no mock could have found were caught by running it against live
systems: the native-gas to ERC-20 fee conversion was wrong by 10\*\*6, a misnamed trade limit was
silently ignored by settings, and the guard deployment script used a cheatcode this forge build does
not implement so it had never run. Two more came from the worker and the Circle path: a refusal
counted as a failure, and a Circle fee shape the adapter could not read.

## How to read the history

The foundational backend landed in one batch and was split afterwards into layer commits (domain,
store, policy, adapters, security, service, api, integrations, tooling) so a reader can follow it in
order.

**`git bisect` is meaningful from the commit that adds `tests/test_workflow.py` onwards.** The layer
commits before it are a reconstruction for reviewability rather than a record of intermediate working
states: the code in them is exactly what landed, but at the time it landed in one commit.

From that commit onwards, each commit is expected to pass `uv run pytest` and `forge test`. A failure
there is a defect in the history rather than an accepted gap.

## What is not verified

Stated in full in the README's limitations. Briefly: no mainnet route for either payment provider,
no screening provider key so every invoice escalates for a human, and the production chart of accounts
has not been reviewed by an accountant.
