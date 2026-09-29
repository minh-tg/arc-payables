# Project history

## Where this started

Tameion began as an empty scaffold at the start of the event window: Canteen's Arc starter
workspace (`Counter.sol`, `foundry.toml`, `package.json`) and two commits. Everything else in this
repository was built during the window, and the boundary is recorded here plainly because the work
is judged on the delta rather than on a snapshot.

| | At the start | Now |
| --- | --- | --- |
| Commits | 2, scaffold only | every commit dated inside the event window |
| Tests | none | 320 Python and 21 Solidity, all hermetic |
| Contracts | the starter `Counter.sol` | `PaymentGuard`, deployed by an Arc-Testnet-only script |
| Integrations | none | Circle Developer-Controlled Wallets, ERPNext over REST, and a credential-free executor that runs the real guard |
| Verified against live systems | nothing | the ERPNext read and import path and the operator bootstrap; Arc Testnet read-only facts |

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

Stated in full in the README's limitations. Briefly: no live Arc payment has been executed, the
ERPNext Payment Entry writeback has not run against a real instance, and address screening has no
vendor configured, so it returns `UNAVAILABLE` and requires human review.
