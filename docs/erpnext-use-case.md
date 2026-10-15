# Use case: paying ERPNext supplier invoices on Arc

A company keeps its payables in ERPNext. Arc Payables reads a submitted Purchase Invoice, checks
it against the Supplier, Purchase Order and Purchase Receipt, pays it in USDC through the
`PaymentGuard` contract on Arc Testnet, and books the result back into ERPNext. This page covers
that path only. The decision layer and the contract are described in the [README](../README.md).

**Scope: a disposable ERPNext v15 sandbox and test USDC.** It is not a production deployment and
no business uses it. The [gates at the end](#what-is-not-done) are the work that remains.

## What ERPNext holds, and what the agent trusts from it

| ERPNext record | Used for | Rule |
| --- | --- | --- |
| Purchase Invoice | amount, currency, lines, due date, state | The amount and lines are compared with the captured invoice. Any difference is a hard block. Only a submitted invoice whose open balance equals its grand total is payable. |
| Supplier | the payee wallet | The wallet comes only from the Supplier custom fields (`custom_usdc_wallet_address`, `custom_usdc_wallet_verified`). An address printed on an invoice is evidence, never a destination. A disabled or on-hold supplier blocks payment. |
| Purchase Order, Purchase Receipt | three-way match | A missing or inconsistent document blocks payment until a reviewer waives it. A waiver never changes the destination or the amount. |
| Payment Entry | the result | Created after settlement, for exactly the supplier amount, allocated to the invoice, no deductions. `reference_no` is `ARC-TESTNET:<tx hash>`. |
| Journal Entry | the network fee | A separate entry, keyed `ARC-TESTNET-FEE:<tx hash>`, so the fee never reduces what the supplier receives. |

The writeback runs through a REST user that is not Administrator (the connector refuses
Administrator on every session). Retries search for the transaction-hash reference first and
verify party, invoice, accounts and amounts before reusing a document, so a lost response cannot
book a payment twice.

## Run it

Stages 1 to 4 are one-time sandbox setup and are described in
[deploy/erpnext/README.md](../deploy/erpnext/README.md). They need you at the keyboard: the site
password, the setup wizard and the API user are all prompted locally and are not scripted.

1. Start the sandbox and create the site and the USD company (runbook steps 1 to 4).
2. Create a scoped API user and export its credentials in your own terminal:
   `FRAPPE_URL`, `FRAPPE_API_KEY`, `FRAPPE_API_SECRET`, plus the `FRAPPE_*` account mapping that
   the bootstrap prints.
3. Check the connection and the mapping:
   `bash script/erpnext-use-case.sh check`
4. Preview, then create the demo records (a supplier with an unverified wallet and an unpaid
   invoice):
   `bash script/erpnext-use-case.sh provision`, then again with `--apply`
5. In ERPNext, a person verifies the supplier wallet. This is deliberate and is the only way a
   payment destination changes.
6. Import and evaluate the invoice, as in [DEMO.md](../DEMO.md#the-flow) (`POST /invoices/import`,
   then `/evaluate`).
7. Preflight, then pay:
   `bash script/erpnext-use-case.sh settle <local-invoice-id>`, then again with `--confirm`.
   This is the step that spends test USDC.
8. Verify what ERPNext holds for that transaction, without trusting the application:
   `bash script/erpnext-use-case.sh verify <tx-hash> <Purchase Invoice name>`

Step 8 is read-only. It checks that the Payment Entry is submitted, pays a supplier, and has
paid, received and allocated amounts that agree with no deductions. It checks that the Purchase
Invoice is settled with nothing outstanding, that the fee journal is submitted and balanced, and
that the ledger rows of both documents balance. A check is skipped, not passed, when the runtime
user cannot read the General Ledger. The result is `consistent` or `NOT consistent`, with the
exit code to match.

## What is verified

- Offline: the adapter, writeback, retry, fee and verifier paths run in the test suite against a
  fake REST server (`uv run pytest tests/test_frappe_adapter.py tests/test_erp_verify.py`). The
  fake mirrors behaviour found on the real sandbox, such as the submit contract in Frappe v15.
- Live sandbox, recorded earlier: ERPNext 15.121.5. One 0.01 test-USDC payment is in the
  [evidence packet](submission-evidence.md): a submitted Payment Entry allocated to a Paid invoice,
  a submitted fee journal, and balanced ledger rows. `HISTORY.md` and the sandbox runbook record
  further runs.
- Two defects found while preparing this page are fixed: a partly paid invoice was treated as
  payable although the workflow pays the full amount, and a late retry booked entries on the day
  of the retry instead of the settlement day.

## What is not done

These are the reasons this is a sandbox integration and not a production one.

- **No customer.** No business has onboarded and no real invoice has been paid.
- **Chart of accounts.** The mapping is illustrative. An accountant has not approved the
  accounts, the 1:1 USDC to USD treatment or the round-up fee booking.
- **Fee treatment.** Whether sponsored gas is the business's expense is unresolved. The fee
  journal for the recorded run does not match the measured fee, and the application's own audit
  record for that run is missing.
- **Runtime user.** Administrator is refused, but the permission set of the real runtime user has
  not been reviewed against least privilege.
- **Partial payments** are blocked, not supported. A credit applied to an invoice lowers its open
  balance and blocks payment. An invoice in a currency other than the configured one is refused.
- **Screening** is not read from ERPNext. The ERP path reports it as unavailable.
- **Not run against a production ERPNext**, a hosted instance or a managed database.
