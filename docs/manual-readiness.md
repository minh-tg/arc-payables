# Manual readiness testing

**Gate:** complete the mock checks below before asking a business to try the product or reporting traction.
**Status:** owner feedback is being addressed; the full gate has not been signed off.
Automated tests and API smoke checks are not manual sign-off.

Budget about 25 minutes. These checks need no Circle, ERPNext, screening or model credentials.
The default demo uses deterministic heuristics, not an LLM. Fixture screening is not a vendor check.

## Automated prechecks (not sign-off)

With uv, agent-browser and Chromium available:

```sh
bash script/console-journey-smoke.sh
bash script/manual-failure-profiles-smoke.sh --screening-unavailable
bash script/manual-failure-profiles-smoke.sh --high-reserve
```

These use fresh mock data and isolated loopback servers/browser sessions. They cover keyboard
cancellation/focus return, empty/reloaded views, settlement refresh/history, explicit audit verification
and both failure-profile screens. The journey includes a separate synthetic worker-history fixture
after its one-payment baseline. Failure profiles evaluate only: no approvals or payments.
Passing them does **not** sign off M01–M16 or establish adoption, finance approval or production safety.

Save any observations before stopping an old manual session, then restart the launcher and refresh
the browser after code changes; Python explanation text requires a fresh server.

## 1. Start an isolated session

From the repository root:

```sh
uv sync --frozen
bash script/manual-readiness-demo.sh
```

Leave that terminal running. Open **http://127.0.0.1:8097/console/** in a fresh private browser window.
Use this URL, **not the existing app on port 8000**.

The launcher:
- Forces mock payment and accounting adapters, fixture screening and heuristics.
- Discards inherited environment credentials and starts outside the project's `.env` directory.
- Creates a new temporary database and backup directory for every run.
- Binds only to loopback, starts no worker, and leaves existing services/data untouched.
- Deletes its disposable files when it exits. Save your observations before pressing Ctrl-C.

If 8097 is occupied, stop only this launch attempt and retry:

```sh
PORT=8099 bash script/manual-readiness-demo.sh
```

Use the replacement port in every browser URL and command below. Never stop an unrelated process.

In a second terminal, confirm the deployment **before clicking any payment action**:

```sh
curl --fail --silent http://127.0.0.1:8097/setup | python3 -c '
import json,sys
d=json.load(sys.stdin)["deployment"]
assert d["mode"] == "demo"
assert d["payment_provider"] == d["accounting_provider"] == "mock"
assert d["screening_provider"] == "fixture"
print(d)
'
curl --fail --silent http://127.0.0.1:8097/payments
```

**Pass:** deployment says demo/mock and payment count is **0**. The console labels simulation/demo.
**Stop:** any provider says Circle, local-key or Frappe, or the safety assertion fails.
No command in this guide deploys a contract, signs a live permit or sends real/testnet funds.

## 2. Baseline walkthrough

The API states/codes below are stable reference points; the UI may display friendlier labels.

| ID | Do this | Expected result |
| --- | --- | --- |
| M01 | Open Overview, Queue, Settlements/Payments, Audit, Worker, Treasury and Setup before loading examples. Refresh the page. | No broken views, no automatic payment or evaluation. Worker status does not claim a pass happened when none has. |
| M02 | Click **Load demo invoices**. Open Queue. Refresh again. | Two synthetic invoices appear. Reloading does not duplicate/reset records or move money. |
| M03 | Open `#/invoice/demo-invoice-suspicious`; click **Evaluate**. Expand the failed evidence checks. | State `ESCALATED`, action `ESCALATE`. Source amount/lines disagree with trusted accounting; `accounting_invoice_match` is a non-overridable failure. UI asks you to correct evidence rather than offering payment or an approval that fixes this hard block. |
| M04 | Compare that invoice's claimed payee with the supplier's trusted wallet. | The malicious `0xdeadbeef…` address is evidence, not a selectable destination. You cannot type a replacement recipient in the payment flow. |
| M05 | Open `#/invoice/demo-invoice-legitimate`; click **Evaluate**. Inspect rationale and evidence. | State `ELIGIBLE`, action `PAY_NOW`; evidence checks pass. Rationale explains the decision. It identifies heuristics, not invented model deliberation. Evaluation alone creates no payment. |
| M06 | Open the clean invoice's payment review. | Dialog explicitly says **simulated payment**; amount is **250 USDC** and recipient is **`0x1111111111111111111111111111111111111111`**. Both match trusted evidence, not arbitrary invoice text. |
| M07 | Cancel using Escape; reopen and cancel with the visible cancel control. | Dialog closes, focus returns sensibly, control works again, and payment count stays **0**. Nothing was submitted by cancellation. |
| M08 | Reopen, confirm the **simulated** payment once, and wait for completion. | State reaches `ERP_RECORDED`; settlement/accounting links appear and payment count becomes **1**. This is mock ledger writeback, not an ERPNext or on-chain transaction. |
| M09 | Refresh the invoice, leave and return, and inspect the payment report. | Still exactly one recorded payment. No second payment control is offered for the settled invoice. Invoice, amount, trusted recipient and simulated accounting records agree. |
| M10 | Click **Verify history** / **Verify audit**. Open Audit and verify again. | Integrity reports intact/success only after verification. The backend audit report has `ok: true`, signed entries and the configured demo signer anchor. |

Check counts after M03/M05/M07/M08/M09 in the second terminal:

```sh
curl --fail --silent http://127.0.0.1:8097/payments | python3 -c '
import json,sys
r=json.load(sys.stdin)
print("Recorded payments:", r["count"])
'
```

After the simulated payment, independently inspect the API report:

```sh
curl --fail --silent http://127.0.0.1:8097/invoices/demo-invoice-legitimate
curl --fail --silent http://127.0.0.1:8097/audit/verify
```

Keep M08/M09 under observation until the UI finishes. If there is a timeout or ambiguous result,
inspect the existing record; do not keep clicking payment or assume failure means nothing happened.

## 3. Failure profiles: no approvals, no automatic spending

Press Ctrl-C in the launcher terminal after saving baseline results. Each command below creates a
**different fresh database**. Clear the previous private browser session before each profile.

### Screening outage

```sh
bash script/manual-readiness-demo.sh --screening-unavailable
```

The launcher preloads synthetic invoices. The guided load-demo panel may be absent because fixture
screening is no longer enabled; open the Queue or the direct clean-invoice URL instead.

- [ ] **M11:** Evaluate `#/invoice/demo-invoice-legitimate`.
- **Pass:** it escalates; `screening_unavailable` explicitly requires human review.
  There is no unattended/pay-now path without approval and payment count remains 0.
- **Do not record an approval here.** This check is about the default refusal/review path, not about
  exercising your authority to acknowledge an exception.
- Do not describe this injected outage as a live screening-provider exercise.

### Treasury reserve pressure

Stop the outage session, then run:

```sh
bash script/manual-readiness-demo.sh --high-reserve
```

Again, invoices are preloaded.

- [ ] **M12:** Evaluate the clean invoice and open the payment queue/plan.
- **Pass:** `cash_reserve` fails, the invoice escalates rather than being automatically payable,
  and the displayed allocation does not silently spend below the configured reserve. Count remains 0.
- The reserve exception is reviewable in the current policy. **Do not approve it in this check.**
  Passing this test proves the unapproved automation path refuses; it does not prove reserve checks
  are universally non-overridable.
- Stop the session after saving results. Restart the default command for usability checks if needed.

## 4. Usability and navigation

- [ ] **M13:** Complete the baseline review/cancellation using Tab, Shift-Tab, Enter and Escape.
  Controls have visible focus; the dialog does not strand focus; button names are understandable.
- [ ] **M14:** Use browser responsive mode at desktop width, **390 px** and **320 px**. Inspect the
  Queue, invoice evidence, long hashes, payment report and mobile menu. No horizontal overflow or
  hidden critical controls. Escape closes the menu and returns focus to its toggle.
- [ ] **M15:** Switch quickly between Overview and Queue while requests are loading. Use browser
  Back/Forward and refresh a direct invoice URL. The final route's content wins; no stale view
  replaces it, and loading/error messages do not claim success.
- [ ] **M16:** Enable reduced motion in browser/OS settings. Nonessential animated status indicators
  stop. Status remains understandable without animation or color alone.

Record failures with the page/route, viewport, action sequence, expected/actual result and a redacted
screenshot. Do not bypass a broken control just to complete the checklist.

## 5. Save the result and decide whether the gate passes

Create a private note such as `local-docs/manual-testing-2026-10-12.md`:

```markdown
# Manual readiness result
Date/time:
Commit:
Browser/version:
Operating system:
Profiles: baseline / screening unavailable / high reserve

| Check | Pass / Fail / Not run | Evidence / issue |
| --- | --- | --- |
| M01–M16 (one row per check) | | |

Baseline payment count before / after cancellation / after confirmation:
Outage-profile payment count:
High-reserve-profile payment count:
Audit verification result:
Blockers:
Tester:
Gate: PASS / FAIL / INCOMPLETE
```

Save screenshots of the hard block, confirmation, recorded payment and audit result. Keep synthetic
data labeled as such. Copy observations before exit; do not publish private browser logs or secrets.

**Gate passes only when M01–M16 pass and all safety failures are resolved.**
Not-run checks leave the gate incomplete. Do not count this synthetic walkthrough as customer traction.

## Later, with owner authorization: real integration acceptance

These checks are deliberately deferred, not silently marked complete:

- Recover the original application payment/audit export. Verify its invoice/payment ID/evidence hash,
  approval attribution, Circle operation(s), settlement hash and ERP document links against
  [the saved evidence packet](submission-evidence.md). If records are unavailable, retain the gap.
- Inspect the existing Circle settlement and ERP invoice/payment/journal/GL documents read-only.
  No new payment or replay is required to inspect the existing run.
- Resolve aggregate fees and ultimate sponsorship with provider evidence and finance review.
- Exercise a real screening provider, actual ERP runtime permissions and trusted supplier onboarding.
- If testing OIDC, exercise the selected real IdP's MFA, forbidden role actions, CSRF, logout,
  revocation and deprovisioning separately. Demo mode has no real MFA or maker/checker identity.
- Any new capped live **testnet** payment requires a separate reviewed plan, explicit operator
  approval and a retained database/audit export. Nothing in this manual gate authorizes one.

A safe mock walkthrough is a prerequisite to a pilot, not production certification.
