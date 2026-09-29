# Contributing

## Ground rules

* **One logical change per commit, and per pull request.** If the message needs the word "and",
  it is probably two changes.
* **Tests come with the change, in the same commit.** The suites are hermetic: no credentials, no
  funded wallet, no live third party. There is no reason to defer them.
* **Never commit a secret.** No private keys, entity secrets, API keys or tokens, and no `.env`.
  Credentials live in the environment; `.gitignore` already excludes the usual paths.
* **Do not weaken a fail-closed default to make a path convenient.** If a check has to be relaxed,
  make the relaxation explicit and configurable, keep the strict behaviour the default, and say why
  in the message.
* **A test that cannot fail is worse than no test.** If an assertion cannot distinguish correct
  from incorrect behaviour, delete it or make it discriminating.

## Working

```bash
uv sync                     # dependencies, pinned by uv.lock
uv run pytest -q            # Python suite
forge test                  # Solidity suite
forge build && forge lint   # contracts must build and lint clean
uv run arc-payables-verify-arc   # read-only Arc Testnet facts; needs no credentials
```

Trunk-based: branch from `main` with a short, descriptive name (`feat/operator-console`,
`fix/budget-rollover`), keep it short-lived, and open a pull request. CI must be green before
merge.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/): `type(scope): summary`, imperative
and lower case, under ~72 characters. Use the body to explain **why** the change is correct and
what it cost to get right, including the alternative you rejected and any defect the tests caught
along the way. That context is the part that is expensive to reconstruct later.

Types in use: `feat`, `fix`, `chore`, `docs`, `test`, `ci`, `refactor`, `perf`, `build`.

```
feat(contracts): budget limits the agent cannot exceed, fixed at deployment

PaymentGuard previously bounded a single payment. It now also enforces cumulative
budgets, all immutable and set at deployment so no key can raise them afterwards.
...
Verified: 19 Foundry tests, including boundaries at the exact cap and epoch rollover.
```

## Review expectations

A change is done when the tests pass, the behaviour is documented where a reader would look for it
(there is no separate docs site), and any limitation it introduces is written down rather than
implied. If something is unverified against a live system, say so in the pull request description.
an honest gap is cheaper than a false claim.
