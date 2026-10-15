#!/usr/bin/env python3
"""Read the paymaster field of one Arc Testnet UserOperationEvent.

Read-only JSON-RPC only. No credentials, no Circle API call, no transaction is sent. The check
answers one narrow question: does the documented public receipt carry a nonzero paymaster in the
standard event? It does not claim who owns, funds or bills that paymaster.

    uv run python script/check-paymaster.py [--transaction HASH] [--rpc-url URL] [--full-hashes]

Exit codes: 0 the paymaster is present and nonzero, 1 a verified negative, 3 the receipt could
not be read.
"""

from __future__ import annotations

import argparse

from arc_payables.settings import get_settings
from arc_payables.verify_arc import DEFAULT_PAYMASTER_TRANSACTION_HASH, RpcClient, check_paymaster


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check the paymaster field in an Arc Testnet UserOperationEvent (read-only, no credentials)"
    )
    parser.add_argument("--transaction", default=DEFAULT_PAYMASTER_TRANSACTION_HASH, help="Transaction hash to read")
    parser.add_argument("--rpc-url", default=None, help="Override the configured RPC URL for this check")
    parser.add_argument("--full-hashes", action="store_true", help="Print transaction hashes in full")
    args = parser.parse_args()

    settings = get_settings()
    rpc = RpcClient(args.rpc_url or settings.circle_rpc_url)
    check = check_paymaster(rpc, args.transaction)
    for line in check.lines(full_hashes=args.full_hashes):
        print(line)
    raise SystemExit(check.exit_code)


if __name__ == "__main__":
    main()
