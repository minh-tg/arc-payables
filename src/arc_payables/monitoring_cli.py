"""Re-screen counterparties from the command line."""

from __future__ import annotations

import argparse

from .api import create_app
from .monitoring import rescreen_suppliers
from .settings import get_settings


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-screen counterparties with open invoices")
    parser.add_argument("--force", action="store_true", help="Re-screen even if the interval has not elapsed")
    args = parser.parse_args()
    app = create_app(settings=get_settings())
    outcomes = rescreen_suppliers(app.state.workflow, force=args.force, source="cli")
    if not outcomes:
        print("No counterparties with open invoices.")
        return
    for item in outcomes:
        marker = {"downgrade": "LOWER", "upgrade": "RAISED", "first_check": "FIRST", "unchanged": "SAME"}[item.direction]
        print(f"  {marker:6} {item.supplier_id}  {item.status} (tier {item.tier})")
        print(f"         {item.detail}")
        if item.invoices_annotated:
            print(f"         recorded on {item.invoices_annotated} open invoice(s)")
    lowered = [item for item in outcomes if item.direction == "downgrade"]
    print(f"Result: {len(outcomes)} counterparties checked, {len(lowered)} with increased risk")
    raise SystemExit(0)


if __name__ == "__main__":
    main()
