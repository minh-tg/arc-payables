# Consenting pilot plan — after manual acceptance

**Gate:** Finish M01–M16 in [manual-readiness.md](manual-readiness.md), save dated owner
sign-off and resolve safety failures **before recruiting users or counting traction**.

## Start with usability, not live finance

Choose one real consenting business workflow; the owner's business may qualify.
A real participant may try their workflow with permitted/redacted examples in mock mode.
Explain that payments, accounting and screening are simulated. A demo viewing, synthetic
batch or controlled wallet is not verified customer usage.

Record privately before access:
- Participant role, workflow, actual problem and consent.
- Data owner, allowed scope, redaction, storage/retention and permission to publish.
- Measured baseline process/time, event-window dates and before/after evidence.
- Operator responsible for escalation and stopping confusing/unsafe behavior.
- Test mode and known limitations.

Use [the disposable launcher](../script/manual-readiness-demo.sh).
No real invoices/PII without permission, live credentials or ledger changes.
Save observations before stopping; temporary data is deleted.

## Measurement record

Keep actual records under ignored `data/` or `local-docs/`, not public examples.

| Measure | Evidence required |
| --- | --- |
| Genuine participant/workflow count | Consented use, purpose and date; separate from viewers |
| Invoices/actions | Actual observed work; classify synthetic/redacted examples |
| Decisions/escalations | Outcomes and reasons, with simulation clearly labelled |
| Time saved | Comparable measured baseline and pilot timing |
| Verified business USDC volume | Relevant confirmed business settlements; exclude mock volume and unrelated sandbox transfers |
| Product/event-window delta | Dated baseline and after-state, commits and usage evidence |
| Publication | Explicit permission for quotes, identity, metrics and screenshots |

Mock usability can show genuine product interaction, **not real payment volume**.
Do not attribute the historical 0.01 test-USDC integration example to this pilot automatically.

## Later financial exercises require separate approval

Require operator authorization, trusted supplier verification, screening/exception review,
scoped provider/ERP permissions and accountant-reviewed mappings/fees. Resolve relevant
historical and finance questions before relying on them. Bound amounts and budgets explicitly.
Never resend uncertain payments or enable mainnet. Production still requires independent
release gates; a mock sign-off or pilot is not production approval.
