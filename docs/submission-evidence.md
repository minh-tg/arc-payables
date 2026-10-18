# Submission evidence: one guarded Arc Testnet payment

**Public chain checked:** 2026-10-12T01:36:01.727352+00:00. **Circle checked:** 2026-10-12T02:19:19.587159+00:00. **ERPNext checked:** 2026-10-12T02:37:56.183892+00:00. **Scope:** Arc Testnet, test USDC, disposable ERPNext sandbox. This packet is integration evidence, not proof of customer adoption, mainnet use or production readiness.

## Start here

- [Settlement transaction](https://testnet.arcscan.app/tx/0x007678e3a08575390a39b8a77ac7598847db3ca7f0fb8a01eae39a1b61741db0)
- [Guard deployment transaction](https://testnet.arcscan.app/tx/0xb0e3b5fbc6a41db4dd6243b7454fe9dde31d305674f9e641f8f43d0d005139fe)
- [Guard address](https://testnet.arcscan.app/address/0xbe0477081f90e68b6699a585d5d31ad93d96f318)
- [A second real payment, from an ERPNext bill, with the application's own record (17 October)](live-erpnext-payment.md)
- [Saved public RPC requests, responses and decoded findings](evidence/arc-testnet-settlement.json)
- [Redacted Circle transaction and SCA wallet response excerpts](evidence/circle-settlement.json)
- [Redacted ERPNext invoice, payment, journal and GL snapshot](evidence/erpnext-sandbox-settlement.json)
- [Public allowance history and per-operation gas metering](evidence/arc-testnet-allowance-history.json)
- [Scoped local application-record availability check](evidence/application-record-availability.json)
- [Live-model decision-layer run (mock providers, no funds)](decision-layer-evidence.md)
- [Reported ERPNext run](../deploy/erpnext/README.md#writeback-verification-on-this-sandbox)
- [Security and integration limitations](../README.md#security-and-integration-limitations)

Explorer links are inspection aids. Public-chain verification used the RPC, Circle verification used authenticated GET requests, and ERPNext verification used an operator-side snapshot from the existing local sandbox in a database-enforced read-only transaction. Credentials and authorization headers are excluded from saved evidence; no permits were signed, payments submitted or ledger records changed.

## Evidence labels

- **Verified:** directly observed in public RPC, authenticated Circle responses or the read-only ERPNext sandbox snapshot, or directly inspected in repository source/documentation; the table names which.
- **Inferred:** an interpretation of observed facts, not a separately demonstrated result.
- **Unverified:** reported behavior for which this packet does not contain independent supporting artifacts.

A successful transaction receipt does not prove Circle account provenance, accounting writeback or a genuine customer obligation.

## 1. Independently checked public-chain facts

Endpoint: `https://rpc.testnet.arc.io`. Full receipt responses and exact getter calldata are in the [JSON snapshot](evidence/arc-testnet-settlement.json).

| Fact | Result | Evidence |
| --- | --- | --- |
| Network | **Verified:** chain ID `0x4cef52` = `5042002` | `eth_chainId` |
| Guard deployment | **Verified:** successful receipt at block **64882475**; created `0xbe0477081f90e68b6699a585d5d31ad93d96f318` | Deployment receipt, `status = 0x1`, `contractAddress` |
| Settlement | **Verified:** successful receipt at block **64909993** | Settlement receipt, `status = 0x1` |
| Guarded payment | **Verified:** one matching `PaymentExecuted` event emitted by the guard | Event address, signature topic, payer, recipient, token and amount |
| Supplier transfer | **Verified:** one matching USDC `Transfer` event from the same payer to the same recipient for **10000 units** | Token address `0x3600000000000000000000000000000000000000`, decoded event |
| Token precision | **Verified:** `decimals() = 6`; 10000 units = **0.01 USDC** | Token `eth_call` and event amount |
| Runtime code | **Verified:** guard has 4615 bytes of runtime code | `eth_getCode`; recorded Keccak-256 below |

The guard event identifies:

| Field | Value |
| --- | --- |
| Payer | `0x784fcf77d0f718210dbadbce1a464bcb67e58aed` |
| Recipient | `0x51015d1a39c0d23741ad860337cfca171696d875` |
| Token | `0x3600000000000000000000000000000000000000` |
| Payment ID | `0x42a0237393088b5d5fcb05b25ed6d4c1fa14a075a2a607b74254c14f161e18bc` |
| Evidence hash | `0xbe12dbb2b07eed379aa9e5be8d55284d3f065495c57c869d42588ea3d919cf85` |
| Authorized/executed event amount | `10000` token units / `0.01 USDC` |

The transaction's outer sender is a relayer, and its outer destination is an account-abstraction entry point. Neither is the event's payer or guard. Use the guard's event and matching USDC transfer, not `transaction.from`, to identify the payment.

A matching transfer event establishes the recorded transfer amount. This check did **not** compare historical recipient balances before and after the transaction.

### Guard configuration observed at check time

These are `eth_call` getter results at `latest`, not historical-state reads at the settlement block.

| Getter | Observed value | Human-readable value |
| --- | --- | --- |
| `perPaymentCap()` | `50000` | 0.05 USDC |
| `epochCap()` | `200000` | 0.20 USDC |
| `recipientEpochCap()` | `50000` | 0.05 USDC |
| `epochLength()` | `86400` | 24 hours |
| `paymentToken()` | `0x3600000000000000000000000000000000000000` | Expected USDC interface |
| `policySigner()` | `0x07158df0ee6eab7d3cf1e0bc386ce479b3ea91a5` | Configured signer address |
| `pauser()` | `0xe67b0288db86abd9d072d8909017cc625976b9b8` | Configured pause authority |
| `paused()` | `0` | Not paused at check time |

Runtime-code Keccak-256: `0xc476339ba476485302b0f252f16c5bbd8c08609233e5cab762ab0f201c7b053f`.

The raw bytecode response is not included in the snapshot; its observed size and digest are recorded, and a reviewer can retrieve it with `eth_getCode`. This check did **not** establish source-to-deployed-bytecode equivalence, historical balances, or an independent security audit.

## 2. Verified Circle provenance and sandbox accounting linkage

Authenticated **GET** requests to `https://api.circle.com/v1/w3s` returned HTTP 200 for the transaction and its wallet. The [redacted response excerpts](evidence/circle-settlement.json) retain allowlisted fields and request paths, not credentials or unrelated account data. The provider operation hash matches the public-chain receipt; the wallet address matches the guard event's payer.

The [ERPNext snapshot](evidence/erpnext-sandbox-settlement.json) was retrieved from the existing disposable `tameion.localhost` site, ERPNext `15.121.5` / Frappe `15.121.1`, using the existing container API socket. Queries ran in a database-enforced read-only transaction and were rolled back. The snapshot verifies current document state and transaction references, not production accounting assurance or least-privilege runtime-user access. The original application's audit/state record remains **Unverified**.

| Artifact | Evidence status and result | Source |
| --- | --- | --- |
| Circle operation | **Verified:** `f419795e-6677-5131-85e9-f7db1f429807`, `COMPLETE`, `ARC-TESTNET`, `CONTRACT_EXECUTION`, matching settlement hash | Authenticated transaction GET |
| Circle wallet | **Verified:** `40500e36-66ab-58e2-bf0f-bd818ca42029`, address `0x784fcf77d0f718210dbadbce1a464bcb67e58aed`, `ARC-TESTNET`, `SCA`, core `circle_6900_singleowner_v4` | Authenticated wallet GET |
| Operation fee | **Verified provider field:** `networkFee = 0.010246706331995826` for this operation; the settlement's standard `UserOperationEvent` meter encodes the same native-USDC amount | Authenticated transaction GET and public receipt |
| Allowance history | **Verified public logs:** two 0.01-USDC approvals by this wallet to this guard in the checked range; the later approval's nonce immediately precedes settlement | Public logs and receipts |
| Protocol gas payer | **Verified decoded event field:** a nonzero paymaster on the checked wallet operations; its ownership, funding and business reimbursement are not verified | Public `UserOperationEvent` logs |
| Purchase Invoice | **Verified in sandbox:** `ACC-PINV-2026-00015`, submitted, USD 0.01, Paid, outstanding amount 0 | Read-only ERPNext snapshot |
| Payment Entry | **Verified in sandbox:** `ACC-PAY-2026-00012`, submitted, paid/received amount 0.01, allocation to this invoice, zero deductions, full matching settlement reference | Read-only ERPNext snapshot |
| Fee Journal Entry | **Verified in sandbox:** `ACC-JV-2026-00006`, submitted, $0.02 debit/credit, full matching fee reference, measured 0.010247 retained in its remark | Read-only ERPNext snapshot |
| General ledger | **Verified in sandbox:** two uncancelled payment rows balance at $0.01; two fee rows balance at $0.02 | Read-only ERPNext snapshot |
| Supplier wallet | **Verified current sandbox state:** verified flag 1 and address matches the public-chain recipient; historical onboarding process not verified | Read-only ERPNext snapshot |
| Workflow result | **Unverified:** reported `ERP_RECORDED` | [README limitations](../README.md#security-and-integration-limitations) |

**Fee interpretation:** under the repository's native-USDC fee model, the returned scalar rounds up to **10247 six-decimal units**, or **0.010247 USDC**. The snapshot confirms a USD company ledger and 1:1 demo USDC/USD exchange rates; rounding up again to a USD cent produces **$0.02** under that mapping. The submitted fee journal and its GL rows actually book that amount. This verifies the recorded posting and arithmetic, not the economic validity of expensing a fee whose ultimate payer is unverified. The saved provider fee is for this operation; the complete approval/reset/guard cost remains unverified.

The checked public `UserOperationEvent` logs name paymaster `0x03df76c8c30a88f424cf3cbbc36a1ca02763103b`, rather than a null paymaster. Under the [standard EntryPoint ABI](https://raw.githubusercontent.com/eth-infinitism/account-abstraction/v0.7.0/contracts/interfaces/IEntryPoint.sol), a non-null paymaster pays the request at the protocol level. It does not identify that paymaster's owner or funder, prove Circle's ultimate billing arrangements or establish a business-paid expense. `uv run python script/check-paymaster.py` re-reads that field from the public receipt and exits nonzero when the receipt cannot be read or names no usable paymaster. The funding source and accounting treatment of the fee journal still need evidence and accountant review.

Do not mix this run with the earlier 250 USD throwaway-chain accounting test, the different worker settlement, or the separately documented first Circle transaction that settled but was initially unbooked. An earlier unbound “one micro-USDC” note does not describe this settlement and has been removed from the runbook.

### Allowance, metering and application-record follow-up

The [public allowance snapshot](evidence/arc-testnet-allowance-history.json) preserves three bounded `eth_getLogs` requests spanning guard deployment through settlement, plus three receipt reads. Both returned approvals are for 10000 ERC-20 units. The later approval, [`0x34a45b…e3500a`](https://testnet.arcscan.app/tx/0x34a45b1a6f8d9e006863fea6083b46ea4bf207cadef3e100c8c5604ec1e3500a), is successful at block 64909985 with wallet nonce 2; settlement is eight blocks later with nonce 3. No zero-value reset Approval event returned in this payer/guard/block-range filter. That is not proof that no other operations or costs exist.

**Verified decoded meters:** the later approval encodes native gas cost 11431048660681230 units (0.011431048660681230 under Arc's 18-decimal native-USDC interpretation), and settlement encodes 10246706331995826 units (0.010246706331995826), matching the saved Circle settlement fee. These are per-UserOperation meters, not the whole bundled transaction fee.

**Inferred pair, not verified application attribution:** same wallet, guard, allowance amount and consecutive nonces strongly suggest the later approval precedes this payment. Their exact metered sum is **0.021677754992677056 USDC**. Rounding the sum upward once gives 21678 six-decimal units; summing separately rounded stages gives 21679. Either rounds upward to **$0.03** at the demo's 1:1 USD mapping, whereas the observed journal books **$0.02**, consistent with settlement-only rounding. Exclude the earlier nonce-0 approval from this inferred pair. Do not call the existing journal an aggregate-fee proof or change it based on this inference; provider/application linkage and accountant review are still required.

The [application availability check](evidence/application-record-availability.json) found no matching settlement, payment-ID or evidence-hash identifiers in the current local payment/audit tables, and `GET /payments/{transaction_hash}` returned 404. Bounded project backup/temp and exact app-volume discovery found no alternate live-run database. This is a scoped lookup failure, not proof the historical run never happened or that records do not exist elsewhere. The next required artifact is the **original live-run database backup or redacted payment/audit export**, including approval acknowledgements and provider operation/fee-breakdown links. Do not rebuild this evidence from the mock database.

### Minimal artifacts needed to close this evidence gap

Use read-only exports from the existing run; do not resend the payment to obtain evidence.

- [x] Redacted Circle wallet response: address matches the event payer, network `ARC-TESTNET`, account type `SCA`.
- [x] Redacted Circle operation response: operation ID, `COMPLETE` status and matching transaction hash.
- [ ] Complete application/provider allowance/reset-operation linkage if claiming total settlement cost; public approval meters are saved, but the run's fee breakdown is missing.
- [x] ERP Purchase Invoice export: company, currency, redacted supplier reference, amount and outstanding balance.
- [x] Submitted Payment Entry export: document ID, `docstatus`, invoice allocation, paid/received amounts, zero deductions and the **full** settlement reference.
- [x] Submitted fee Journal Entry and GL exports: company, currencies, exchange rates, voucher links, debit/credit totals and extracted measured-fee remark evidence.
- [ ] Explanation of who ultimately funds the paymaster and why the fee expense is booked; the public protocol-level paymaster field alone is insufficient.
- [ ] Application payment report linking the same invoice, payment ID, evidence hash, payer, recipient, settlement and ERP documents.
- [ ] Recorded approval/audit evidence identifying any human review, especially unavailable screening.

Remove credentials, entity secrets, signing keys, tokens and personal/customer information. Retain the matching public transaction hash and the minimum non-sensitive linkage fields. Public exports need the data owner's permission. Screenshots can help explain the records, but should not replace structured evidence.

## 3. What the safety evidence establishes—and does not

**Verified in source:** [PaymentGuard.sol](../src/PaymentGuard.sol) checks the permit signature, payer, token, expiry and payment ID, and applies configured spending caps before the transfer. Its cap fields are declared immutable; zero-valued caps disable their respective checks. These are source findings, not a substitute for bytecode-equivalence verification.

The contract accepts the recipient in a signed permit; it does **not** hardcode an approved-supplier allowlist. Trusted supplier verification remains an off-chain policy/onboarding boundary. A signer-authorized wrong recipient is not independently recognized as wrong by this contract. The model's authority, wallet permissions and signer isolation must also be reviewed; the existence of the guard is not proof that every possible wallet transfer must pass through it.

**Unverified here:** live sanctions screening, production ERP permissions/account mapping, production SSO/HSM assurance, end-to-end zero-human-intervention behavior, and source-bytecode equivalence. The runbook's `screening_unavailable` acknowledgement concerns the different worker transaction `0x801b2ad9eb90b08e916fa65dea94d2dba70187532f1cc76d102876cccd53333b`; it does not establish the approval history of this Circle payment. Neither run supports a verified fully human-free workflow claim in this packet.

The public receipt demonstrates one guarded settlement, not general resistance to all attacks. See [accounting release gates](accounting.md#gates-that-need-an-accountant-or-a-live-provider) and [settlement assurance](production-settlement.md).

## 4. Recheck without moving money

### Public chain: no credentials

From the repository root, replay the [saved request list](evidence/arc-testnet-settlement.json) against the public endpoint. It contains only receipt reads, chain ID and `eth_call` getters:

```sh
jq '.requests' docs/evidence/arc-testnet-settlement.json | \
  curl --fail-with-body --max-time 30 --silent --show-error \
    -H 'Content-Type: application/json' --data-binary @- \
    https://rpc.testnet.arc.io
```

For a minimal chain-ID check without `jq`:

```sh
curl --fail-with-body --max-time 30 --silent --show-error \
  -H 'Content-Type: application/json' \
  --data '{"jsonrpc":"2.0","id":1,"method":"eth_chainId","params":[]}' \
  https://rpc.testnet.arc.io
```

Require `0x4cef52`. A timeout or HTTP error means **unresolved verification**, not a failed payment. To check the settlement, use `eth_getTransactionReceipt` with the full transaction hash above, require `status = 0x1`, and inspect the guard event plus the matching USDC transfer. Do not accept a receipt status alone as proof of the intended amount or recipient.

### ERPNext sandbox: operator-authorized, read-only

Recheck the three exact document IDs and their GL entries in the existing sandbox using authorized read-only API access or operator-side inspection. Preserve the transaction references, amounts, currencies, document submission states and invoice allocation. This export used the container's existing Frappe database connection in a read-only transaction; it did **not** verify the application integration user's permissions or use/create an ERP API key. Supplier names and raw remarks are omitted from the saved snapshot.

### Existing application: operator-authorized, read-only

On the deployment that retains this run, open **Settlements** and look up the full transaction hash. The [API](../src/arc_payables/api.py) accepts a transaction hash as a payment reference:

- `GET /payments/{transaction_hash}`: recorded decision, authorization, settlement and ledger linkage.
- `POST /payments/{transaction_hash}/verify`: read-only provider/chain recheck; it sends no funds.
- `GET /audit/verify`: backend recomputes stored audit-chain integrity.

Use the deployment's existing authentication; never put secrets in public examples or saved exports. A fresh mock database will not contain these historical records. An intact local audit chain does not independently prove the underlying business obligation or ERP state.

## 5. Evidence-backed submission wording

> Arc Payables checks invoices against trusted accounting evidence and settles eligible payments through a policy-signed guard on Arc Testnet. One 0.01 test-USDC payment is independently observable in a successful receipt with matching guard and token-transfer events. Circle's API confirms the completed contract execution and the matching Arc Testnet SCA payer. The same transaction is referenced by a submitted ERPNext sandbox Payment Entry allocated to a Paid invoice, with a separate submitted fee journal and balanced GL entries. The original application audit record and production-accounting gates remain unverified. This is testnet integration evidence—not mainnet activity, independently verified customer traction, or a production-security claim.

Once the missing exports are actually checked, update the relevant labels and wording rather than removing the limitations wholesale.

**Traction boundary:** no genuine-customer evidence was verified by this packet. Synthetic invoices, a disposable sandbox and a small transfer demonstrate implementation, not adoption. A real internal business workflow may count under the [official rules](https://tameion.thecanteenapp.com/), but requires truthful evidence of the actual business problem, usage and outcome.
