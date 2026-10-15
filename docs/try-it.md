# Try Arc Payables in 15 minutes

This page is for someone who has never seen the project. Everything you run is mock: payments are
simulated, no funds move, no accounting system is written to, and nothing leaves your machine.
It is a prototype. No business uses it, and a trial run is not a pilot.

## Run it

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```
git clone https://github.com/minh-tg/arc-payables
cd arc-payables
uv sync
bash script/manual-readiness-demo.sh
```

Open http://127.0.0.1:8097/console/. The script uses a temporary directory and deletes it when you
stop it with Ctrl-C. If a hosted mock demo link was sent to you, use that and skip the steps above.

## Three things to do

1. **Pay the clean invoice.** Open Payment queue, evaluate `ACME-INV-2026-001`, open it and confirm
   the payment. Then open Settlements and find it. Could you tell what happened and whether the
   books were updated?
2. **Open the held invoice.** Evaluate `ACME-INV-2026-002`. Read the case card and the check that
   failed. Could you tell what to fix, and who would fix it?
3. **Try to make it pay something it should not.** Change the amount, the supplier or the payee
   address, or ask the planner for an unsafe payment. The aim is to find a path to a payment that
   the checks should have stopped. If you find one, that is the most useful result you can send.

## Send what you found

Open a [trial report](https://github.com/minh-tg/arc-payables/issues/new?template=trial-report.yml).
It takes about five minutes. A confusing screen is a finding, not a mistake on your part. The form
asks separately whether your words may be quoted, and the answer defaults to no.

Do not enter real supplier details, credentials or secrets anywhere in the console or the report.

## What a trial shows and does not show

A trial report is evidence that someone outside the project used the prototype and what they made
of it. It is not customer usage, payment volume or a security review, and it is counted that way
in the submission.
