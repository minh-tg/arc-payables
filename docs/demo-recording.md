# Safe recording plan — target 2:40

**Prerequisites:** Owner M01–M16 sign-off and footage/evidence permission/redaction review.
Publish after the intended revision is pushed and new remote CI passes.
No new live payments, ERP writes, credential entry or deployment are needed.

```sh
bash script/manual-readiness-demo.sh
# http://127.0.0.1:8097/console/
```

| Time | Show and explain |
| --- | --- |
| 0:00–0:15 | Overview/Demo label: invoice evidence is not payment authority; this walkthrough moves no funds. |
| 0:15–0:40 | Suspicious invoice: evaluate, show hard accounting mismatch and absent payment control. Approval cannot override conflicting evidence. |
| 0:40–1:05 | Clean invoice: evaluate, review exact 250 USDC/trusted wallet, cancel, then confirm simulation. Recommendation and authorization are separate. |
| 1:05–1:25 | Mock ledger entries and audit verification. These belong to this synthetic walkthrough, not the old Circle payment. |
| 1:25–1:55 | Saved Circle operation, public 0.01 test-USDC receipt and ERP sandbox links. Separate historical example; its app/approval export and ultimate fee expense treatment remain unresolved. |
| 1:55–2:20 | Guard/reserve/escalation boundaries: fixed caps, signature/replay/pause checks; supplier onboarding and signer assurance remain external. |
| 2:20–2:40 | Owner-approved usage evidence, or “adoption not yet verified”; repository link and testnet-prototype limitations. |

Keep **below 3:00**, including opening/closing cards. Do not replay live-run/bootstrap/worker
commands while recording. Default reasoning is heuristics; do not portray it as live LLM or
vendor-screening execution.

Before uploading: refresh/recheck borders and dialog/focus/layout; remove secrets, terminal
history, PII and unauthorized metadata; distinguish mock and historical artifacts; make no
unsupported aggregate-fee/expense or customer claims. Test repository/video access signed out
and retain the actual project-form receipt.
