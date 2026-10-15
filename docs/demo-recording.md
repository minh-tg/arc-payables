# Safe recording plan: target 2:40, hard limit 2:40

**Prerequisites:** manual checks M01–M16 done, the intended revision pushed with green CI, and
permission to show the evidence pages. No new live payment, ERP write, credential entry or
deployment is needed. Everything below is mock data, saved evidence or testnet.

Open three windows before recording and keep them in this order:

1. Terminal at the repo root. Run `bash script/manual-readiness-demo.sh` in a second tab and leave it.
2. Browser, private window, `http://127.0.0.1:8097/console/`.
3. Browser tab with the public settlement: the `Settlement transaction` link at the top of
   [submission-evidence.md](submission-evidence.md).

Voice: a screen recording with a voice-over is enough; no webcam is needed. A synthetic voice is fine (the event page sets no rule on voice), and a caption note such as "narrated with a synthetic voice" in the video description costs nothing. Record the screen first, then fit the narration to it.

Do not run the live-model script while recording. It takes about 35 seconds and needs a key.
`script/show-planner-evidence.sh` prints the saved result from a file in about a second.
`script/decision-eval.py` and `script/adversarial-corpus.py` need no key and no network.

The narration below is the captions in [demo-recording.srt](demo-recording.srt). The SRT file is
the same words split into cues, with the mock labels as separate bracketed cues. `tests/test_captions.py`
checks the line length, reading speed, ordering and the 2:40 cap.

| Time | On screen | Say |
| --- | --- | --- |
| 0:00–0:19 | Console Overview, `Hosted demo · simulated payments & ledger` label visible | "An invoice and an AI recommendation should never be payment authority. Arc Payables lets a model advise. Evidence, a signed permit and a contract with fixed caps decide whether money moves. This walkthrough uses mock payments." |
| 0:19–0:34 | Click **Load demo invoices**, open the suspicious invoice, **Evaluate**, expand the failed check | "This invoice names a different payee and its amounts disagree with the accounting record. That is a hard block. No approval can override it. The model is not asked either, because reasoning cannot supply evidence that is missing." |
| 0:35–1:12 | Terminal: `bash script/show-planner-evidence.sh`, then `uv run python script/decision-eval.py` and `uv run python script/adversarial-corpus.py` | "A saved run against a real model, DeepSeek V4.1 Flash: on a limit trade-off it escalates to a human. It named an instruction hidden in an invoice and ignored it. The scripted fake model ran 39 labelled cases and 150 adversarial variants; with policy on, no hostile answer reached an effective pay decision. Given four payables and 400 USDC, it put the overdue invoice first, unlike the rules. Code computes the spend, so the model only changes the sequence." |
| 1:12–1:27 | Console: open the clean invoice, **Evaluate**, **Review payment**, point at 250 USDC and the trusted wallet, **Cancel**, reopen, **Confirm**, scroll to ledger entries, **Verify audit** | "The clean invoice passes every check. The payee comes from the supplier record, not from the invoice. I can cancel. Confirming records one simulated payment and its ledger entries, and the audit chain verifies." |
| 1:27–1:34 | Hosted console at the deployed URL, header label visible (only if the owner has deployed it) | "The same console also runs on a host: labelled mock mode, and a profile that refuses live providers and secrets." |
| 1:35–2:01 | Explorer page for the settlement transaction, then the `PaymentExecuted` and USDC `Transfer` events; then the evidence page's ERPNext table | "Separately, one 0.01 test-USDC payment went through the guard contract on Arc Testnet from a Circle smart-contract wallet. The explorer shows the guard's event and the matching transfer. An ERPNext sandbox Payment Entry carries the same transaction hash. The application's own audit record for that run is missing, and the evidence page says so." |
| 2:01–2:18 | README top, or the evidence page's limits section | "The guard fixes per-payment, per-epoch and per-recipient caps at deployment, and rejects replays and expired permits. This is a testnet prototype with no customers yet. Adoption is not verified. The repository is github.com/minh-tg/arc-payables." |

The hosted console segment at 1:27 is **only if the owner has deployed it**. Without a deployment, cut
that row and let the next segment start at 1:27; the rest of the timeline and the SRT are unchanged
otherwise. Do not describe the hosted console as a live Circle or ERP system: its profile refuses live
providers and secrets, and it writes to no external service.

Optional extras, if a segment finishes early (each prints about ten lines and touches no live service):

- `bash script/unattended-loop-demo.sh` runs one worker pass with autopay on mock providers and prints the one simulated payment. Say "simulated", not "paid".
- `uv run python script/decision-eval.py` prints the 39-case result of a scripted fake model: policy on against policy off. Say "scripted model", never "model accuracy".
- `uv run python script/adversarial-corpus.py` prints the 250-variant result of the same scripted fake model. Say "250 correlated variants", never "150 attacks".

Keep the video **at or below 2:40**. Do not describe the mock walkthrough as live Circle or ERP
activity. Do not say the model "approved" or "paid" anything: it advises, and the deterministic
policy and the contract decide. The eval and corpus numbers come from a scripted fake model, so
label them that way on screen and in the narration.

Before uploading: remove terminal history and any visible keys or personal information, and
check that every on-screen claim is either mock, testnet, saved evidence or labelled unverified.
Test the repository and the video link signed out, and keep the actual form receipt.
