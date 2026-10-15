# Safe recording plan: target 2:45, hard limit 3:00

**Prerequisites:** manual checks M01–M16 done, the intended revision pushed with green CI, and
permission to show the evidence pages. No new live payment, ERP write, credential entry or
deployment is needed. Everything below is mock data or saved evidence.

Open three windows before recording and keep them in this order:

1. Terminal at the repo root. Run `bash script/manual-readiness-demo.sh` in a second tab and leave it.
2. Browser, private window, `http://127.0.0.1:8097/console/`.
3. Browser tab with the public settlement: the `Settlement transaction` link at the top of
   [submission-evidence.md](submission-evidence.md).

Voice: a screen recording with a voice-over is enough; no webcam is needed. A synthetic voice is fine (the event page sets no rule on voice), and a caption note such as "narrated with a synthetic voice" in the video description costs nothing. Record the screen first, then fit the narration to it.

Do not run the live-model script while recording. It takes about 35 seconds and needs a key.
`script/show-planner-evidence.sh` prints the saved result from a file in about a second.

| Time | On screen | Say |
| --- | --- | --- |
| 0:00–0:15 | Console Overview, Demo label visible | "An invoice document and an AI recommendation should never be payment authority. Arc Payables lets a model advise. Evidence, a signed permit and a contract with fixed caps decide whether money moves. This walkthrough uses mock payments." |
| 0:15–0:40 | Click **Load demo invoices**, open the suspicious invoice, **Evaluate**, expand the failed check | "This invoice names a different payee and its amounts disagree with the accounting record. That is a hard block. No approval can override it. The model is not asked either, because reasoning cannot supply evidence that is missing." |
| 0:40–1:15 | Terminal: `bash script/show-planner-evidence.sh` | "A saved run against a real model, DeepSeek V4.1 Flash. On a limit trade-off it escalates to a human. I hid an instruction in an invoice telling it to pay a burn address and raise the amount to 9999. It named the attack and ignored it. A model that obeyed would still be stopped, because policy runs after it, and a test proves that. Given four payables and 400 USDC, it put the overdue invoice first, unlike the rules. Code computes the spend, so the model only changes the sequence." |
| 1:15–1:50 | Console: open the clean invoice, **Evaluate**, **Review payment**, point at 250 USDC and the trusted wallet, **Cancel**, reopen, **Confirm**, scroll to ledger entries, **Verify audit** | "The clean invoice passes every check. The payee comes from the supplier record, not from the invoice. I can cancel. Confirming records one simulated payment and its ledger entries, and the audit chain verifies." |
| 1:50–2:25 | Browser: explorer page for the settlement transaction, then the `PaymentExecuted` and USDC `Transfer` events; then the evidence page's ERPNext table | "Separately, one 0.01 test-USDC payment went through the guard contract on Arc Testnet from a Circle smart-contract wallet. The explorer shows the guard's event and the matching transfer. An ERPNext sandbox Payment Entry carries the same transaction hash. The application's own audit record for that run is missing, and the evidence page says so." |
| 2:25–2:45 | Repo README top, or the evidence page's limits section | "The guard fixes per-payment, per-epoch and per-recipient caps at deployment, and rejects replays and expired permits. This is a testnet prototype with no customers yet. Adoption is not verified. The repository is github.com/minh-tg/arc-payables." |

Optional extras, if a segment finishes early (each prints about ten lines and touches no live service):

- `bash script/unattended-loop-demo.sh` runs one worker pass with autopay on mock providers and prints the one simulated payment. Say "simulated", not "paid".
- `uv run python script/decision-eval.py` prints the 33-case result of a scripted fake model: policy on against policy off. Say "scripted model", never "model accuracy".

Keep the video **below 3:00** including any title or closing card. Do not describe the mock
walkthrough as live Circle or ERP activity. Do not say the model "approved" or "paid" anything:
it advises, and the deterministic policy and the contract decide.

Before uploading: remove terminal history and any visible keys or personal information, and
check that every on-screen claim is either mock, saved evidence or labelled unverified. Test the
repository and the video link signed out, and keep the actual form receipt.
