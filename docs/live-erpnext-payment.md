# A real payment from an ERPNext bill, 17 October

One bill in a real ERPNext sandbox was imported by Arc Payables, held until a person verified the
supplier's wallet in ERPNext, then paid on Arc Testnet from a Circle wallet through a new
`PaymentGuard`, and written back to ERPNext. The amount is 0.01 USDC of test money.

- Payment transaction: [`0x90e96a…c495c0`](https://explorer.testnet.arc.io/tx/0x90e96a2317a57d3134fe74c0bf2b2e12aebd4ba3da45442e0199fcd7c8c495c0)
- Guard: `0x13120C0ff3687178172Bc16CcB3840EEe55B1650`, caps 0.02 USDC per payment, 0.05 per day, 0.02 per recipient per day, fixed at deployment
- The application's own record, with a verified signed permit and a 12-entry audit chain: [payment-report.json](evidence/live-erpnext-2026-10-17/payment-report.json)
- The independent checks: [independent-checks.json](evidence/live-erpnext-2026-10-17/independent-checks.json)

## Four checks that agree

| Source | Result |
| --- | --- |
| Public chain | Receipt status success in block 67764995. A USDC transfer of exactly 10000 units (0.01 USDC) from the Circle wallet to the supplier address, and two events from the guard. |
| Circle | Operation `fd0eb709-bf7a-5fb9-8477-ba52ae37661c` is `COMPLETE` on ARC-TESTNET, a contract execution against the guard, with the same transaction hash and the configured wallet. |
| ERPNext | `arc-payables-erp-verify` reports `consistent`: Payment Entry `ACC-PAY-2026-00002` submitted and allocated to `ACC-PINV-2026-00003`, which is Paid with nothing outstanding, and a balanced fee Journal Entry `ACC-JV-2026-00001`. |
| The application | The payment is recorded as not simulated, confirmed and settled-and-recorded, and its signed audit chain verifies. |

## What this does not show

- The wallet's balance fell only by the 0.01 paid. The gas was paid by a protocol-level paymaster whose
  funding is not verified, so the 0.41 USD fee booked in ERPNext is the fee Circle reported, not
  something the wallet paid. An accountant has not reviewed that treatment.
- ERPNext supplies no sanctions screening, so the result is unavailable and the policy lowers the automatic
  limit instead of asking a reviewer.
- The sandbox integration user has broad roles, which is not a least-privilege setup.
- The guard was deployed with a newly generated throwaway signer because the first guard's signer key was
  not available. The first guard, and the 12 October payment, are unchanged and documented in
  [submission-evidence.md](submission-evidence.md).
- There is no customer and no real money. It is a testnet prototype.
