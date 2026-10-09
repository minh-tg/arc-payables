# Accounting: fees, screening and what still needs an accountant

Two subjects live here, and they have opposite shapes:

* **Fees** are arithmetic and therefore the code's job. This document records the model after the
  fix that made the booked figure the *whole* cost of settling a payment, not just its last step.
* **Mappings and screening** end in a human judgement. The code can enforce that a judgement was
  made and can refuse to act without one, but it cannot make it. Those are listed as gates, not
  claims.

## The fee model: every operation, not just the settlement

Settling one authorization can take up to three on-chain operations:

| Stage | When it happens | Who pays |
| --- | --- | --- |
| `approve_reset` | only when a stale, non-zero allowance is left over | we do |
| `approve` | whenever the allowance is not already exactly right | we do |
| `guard` | always: the guarded call that moves the money | we do |

Earlier versions booked the **`guard` operation's fee only**. That understated our own cost, and it
did so in the direction that flatters the numbers: the supplier always received the authorized
amount, so nothing looked wrong. On Arc Testnet the missed amount is trivial; the accounting error
is not, and it would have been inherited by any real deployment.

Now each stage is measured as it confirms and the recorded `fee_units` is their **sum**, with
`fee_breakdown` recording where the money went:

```json
{ "fee_units": 5125, "fee_breakdown": { "approve": 1995, "guard": 3130 } }
```

The invariants this rests on, each pinned by a test:

* `fee_units == sum(fee_breakdown.values())` — the total always explains itself.
* Two operations of the same stage (a reset then a new allowance) are **summed**, not overwritten.
* A payment that needed no approval books only the `guard` fee: nothing is invented.
* A payment that fails *after* the allowance was set still records what was spent. A failed payment
  is not a free one.
* A deferred fee is **merged**, never replaced. When the provider cannot yet name the settlement
  fee, the record holds the allowance cost; the later re-read adds the `guard` stage to it. Replacing
  here would silently delete a cost that was already paid.
* The end-to-end case is checked against the chain, not the provider: `test_local_payment.py` sums
  each operation's own receipt (`gasUsed * effectiveGasPrice`) and compares that to `fee_units` and
  to `fee_breakdown`.

The fee is still booked rounded **up** to the company currency's smallest unit, because a ledger in
USD cannot represent a fraction of a cent. The entry's remark records the measured figure; rounding
can overstate our own cost by less than one unit per payment and never understates it.

`arc-payables-reconcile` reads these figures when comparing the books with the chain, and the
Console's Payments view shows the booked fee per payment.

## What is verified, and how

| Claim | Evidence | Limit |
| --- | --- | --- |
| The booked fee is the whole cost of settling | End-to-end test summing both operations' receipts from a real EVM running the real guard bytecode | Arc Testnet gas prices, not production ones |
| A fee the provider reports later is merged, not replaced | Hermetic test on the deferred-fee path | Uses the mock provider's deferral model |
| The ledger can represent the fee | Rounding-up tests and the fee-expense computation | A currency with coarser units rounds more |
| Writeback books the payment and the fee as two documents | Sandbox ERPNext runs recorded in HISTORY.md | Disposable sandbox, not a production chart of accounts |

## Gates that need an accountant or a live provider

These are the reason this area cannot be closed by engineering alone. Each is a release gate for
real funds.

### 1. Accountant-approved chart-of-accounts mapping

The mapping names the company, the paid-from and paid-to accounts, the mode of payment, the fee
account, the cost centre and the exchange rates. Engineering can verify that every value is present,
that the currencies are consistent, and that the figures balance — and it does. It cannot know
whether those are the *right* accounts for this business.

A reference enterprise mapping conforming to US GAAP and IFRS dual-reporting standards is defined
in [`src/arc_payables/fixtures/standard_coa.json`](../src/arc_payables/fixtures/standard_coa.json).

#### Debit/Credit Proof Tables

**Leg 1: Supplier Payable Discharge (Payment Entry)**

When a $250.00 USDC payment settles on Arc, the connector submits an ERPNext Payment Entry:

| Account | Account Number | Root Type | Debit (USD) | Credit (USD) |
| --- | --- | --- | --- | --- |
| Accounts Payable - Trade | 2010 | Liability | $250.00 | $0.00 |
| Digital Asset Treasury - USDC | 1120 | Asset | $0.00 | $250.00 |
| **Total** | | | **$250.00** | **$250.00** |

*Invariants:*
* Zero imbalance: `Debit == Credit == settlement_amount`.
* Outstanding balance on linked Purchase Invoice decreases by exactly $250.00.
* Party ledger matches on-chain recipient proof in `tx_hash`.

**Leg 2: Absorbed Network Gas Fees (Journal Entry)**

When Arc execution expends 0.005125 USDC across approve and guard calls:

| Account | Account Number | Cost Centre | Debit (USD) | Credit (USD) |
| --- | --- | --- | --- | --- |
| Blockchain Network Fees Expense | 6140 | FinOps | $0.01 | $0.00 |
| Digital Asset Treasury - USDC | 1120 | FinOps | $0.00 | $0.01 |
| **Total** | | | **$0.01** | **$0.01** |

*Invariants:*
* Micro-cent gas costs round up to the next full cent ($0.005125 -> $0.01) so ledger books never understate expense.
* Remark field on Journal Entry preserves the unrounded fractional unit figure (`fee_units: 5125`).
* Fee is booked against the FinOps cost centre, keeping operational overhead isolated from supplier COGS.

#### Accounting Review Sign-Off Checklist

Before pointing writeback to production general ledgers:

- [ ] **Chart of Accounts Validation**: All 4 target accounts (Trade AP, Digital Treasury, Gas Expense, Cost Centre) exist with active status in the ERP ledger.
- [ ] **Currency Alignment**: Paid-to account and company base currency match (USD), with digital treasury denominated in USDC.
- [ ] **Rounding Variance Policy**: Finance team accepts round-up booking policy on fractional network gas expenses.
- [ ] **Audit Trail Linkage**: On-chain transaction hash and permit signature digests are stored in Payment Entry reference fields.
- [ ] **Sign-Off Record**: Signed approval sheet from controller or senior accountant attached to deployment release manifest.

### 2. Least-privilege ERP user

The connector needs a user that can read purchase invoices and suppliers, and create a Payment
Entry and a Journal Entry — and nothing else. It must not be an administrator, must not be able to
delete documents, and must not be able to alter submitted entries.

Required evidence: the permission set of the actual runtime user, read from ERPNext, showing only
those grants. A runtime user with administrator rights makes every other control in this document
optional.

### 3. Screening exercised against the real provider

The OpenSanctions provider is implemented behind its interface with deterministic evidence and
fail-closed behaviour, and it is covered by tests. **No live call has been made**, because no key is
configured. A real deployment therefore still relies on human review for every counterparty.

Required evidence: at least one live screening call for a known-clean and a known-flagged
counterparty, with the recorded evidence inspected and the review procedure followed for the
flagged one. Until then the honest description is "screening is implemented and untested against the
vendor".

### 4. Trusted supplier onboarding

A payment destination can only come from the trusted Supplier record, and the record requires a
verified wallet. That check is enforced in code: an unverified wallet escalates rather than paying.

What is *not* programmatically established is who is allowed to verify a wallet, and against what
evidence. Required evidence: a written onboarding procedure naming the verifier, the evidence they
require (screening result, supplier confirmation through a channel already trusted), and the fact
that the person who enters a supplier cannot also be the person who verifies its wallet. That
separation is a process control, enforced by the accounting system or by hand — not by this service.

## Verification commands

```sh
# Fee arithmetic, the merge path and the failure path
uv run pytest -q tests/test_fee_completeness.py tests/test_writeback_fee.py

# The fee measured against real receipts on a real EVM
uv run pytest -q tests/test_local_payment.py -k measured_fee

# The Circle path, including the allowance operation's fee
uv run pytest -q tests/test_circle_executor.py -k settles_the_supplier
```
