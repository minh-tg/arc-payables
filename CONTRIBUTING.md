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

## Testing it by hand

Three commands, no credentials, and nothing leaves the machine. The demo adapters are used unless a
provider is explicitly configured, so everything below is a local simulation.

```bash
uv run arc-payables-seed                 # a payable, a blocked invoice, a treasury and a supplier
uv run uvicorn arc_payables.api:app      # API and console on 127.0.0.1:8000
uv run arc-payables-worker --once --intake --autopay
```

Then open <http://127.0.0.1:8000/console/>. Straight after seeding the console is mostly empty; the
worker pass is what fills the settlement log and the invoice's policy checks. `DATABASE_PATH` names
the database, so `rm -f data/arc_payables.sqlite3` starts over.

Reading, evaluating and paying need no credential locally. Linking an invoice and recording an
approval do: both are refused with `human_approval_not_configured` until `API_KEY` and
`APPROVAL_TOKEN` are set, because approving is a human act and the service will not pretend one
happened.

### Flows worth walking

| Flow | How | What to look for |
| --- | --- | --- |
| Refuse a suspicious invoice | Evaluate `demo-invoice-suspicious` | `ESCALATED` with `payee_mismatch` failing, and the destination shown is the verified supplier record rather than the address on the invoice |
| Pay an authorized one | Pay `demo-invoice-legitimate` | `CONFIRMED`, then `ERP_RECORDED`, with the network fee as its own entry |
| Watch it run without a person | `uv run arc-payables-worker --once --intake --autopay` | The Worker view lists every step and what it declined, with the reason |
| A payable that arrives early | `PAYMENT_DUE_WINDOW_DAYS=-1 uv run arc-payables-worker --once --intake` | The discovered payable parks as `WAITING` with only `payment_timing` failing, which is a decision rather than an oversight |
| Then its due date arrives | `PAYMENT_DUE_WINDOW_DAYS=3 uv run arc-payables-worker --once --intake --autopay` | Intake reconsiders it and it is paid and booked, with nobody evaluating it by hand |
| The record can be checked | The verify button on an invoice | The hash chain recomputes, or it names the first entry that fails |
| Refusals | `uv run pytest -q -k "budget or suspicious or reduced"` | Each one shows the system declining instead of trusting |

The console's guided view is part of what is being reviewed. A new session opens with it on: dotted
terms whose definition opens in place, a plain introduction per view, and the suggested next move
beside anything that names a problem. The `Explaining:` control in the header turns it off, and the
words that explain a code stay either way.

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
