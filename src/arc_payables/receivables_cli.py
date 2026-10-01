"""Sync expected inflows from the accounting system into the local store.

Receivables are additive context, not decisions: this reads open Sales Invoices through the
configured connector and records each one locally, so the forecast can add expected money
back on its expected date. Nothing here authorizes, approves or moves anything.
"""

from __future__ import annotations

import argparse

from .api import create_app
from .settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync expected inflows from open Sales Invoices")
    parser.add_argument("--collect", metavar="EXTERNAL_ID", help="Mark one receivable collected so it leaves the forecast")
    args = parser.parse_args()
    app = create_app(settings=get_settings())
    workflow = app.state.workflow
    store = workflow.store

    if args.collect:
        if store.mark_receivable_collected(args.collect):
            print(f"Collected {args.collect}: it no longer counts as an expected inflow.")
        else:
            print(f"No open receivable {args.collect} was found; nothing changed.")
        raise SystemExit(0)

    try:
        receivables = workflow.accounting.list_receivables()
    except Exception as exc:
        print(f"Could not read receivables: {type(exc).__name__}: {exc}")
        raise SystemExit(1)
    if not receivables:
        print("No open receivables. The forecast counts outflows only.")
        return
    for receivable in receivables:
        store.record_receivable({
            "external_id": receivable.external_id,
            "customer": receivable.customer,
            "reference": receivable.reference,
            "amount_units": receivable.amount_units,
            "currency": receivable.currency,
            "expected_date": receivable.expected_date.isoformat(),
            "source": receivable.source,
        })
        print(f"  {receivable.external_id}  {receivable.customer}  {receivable.amount_units / 10**6} USDC expected {receivable.expected_date}")
    print(f"Result: {len(receivables)} expected inflow(s) recorded for the forecast")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
