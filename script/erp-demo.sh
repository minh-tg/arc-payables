#!/usr/bin/env bash
# A disposable, loopback-only demo with an accounting system beside Arc Payables.
#
#   simulated ERPNext (port 8081)  <--REST-->  Arc Payables console (port 8097)  --> simulated payment
#
# Bills are read from the simulated ERPNext, decided, "paid" by the mock payment provider, and the
# Payment Entry is written back to the simulated ERPNext. The stand-in is not ERPNext (see
# src/arc_payables/sim_erpnext.py), no chain transaction is made, and nothing here uses a credential:
# the keys below are throwaway strings for this process only. ERPNext does not supply sanctions
# screening, so that result is unavailable and the policy handles it with a reduced automatic limit
# (a quarter of the 2000 USDC limit) instead of asking a reviewer for every bill.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8097}"
ERP_PORT="${ERP_PORT:-8081}"
for value in "$PORT" "$ERP_PORT"; do
  if [[ ! "$value" =~ ^[1-9][0-9]{3,4}$ ]] || [ "$value" -lt 1024 ] || [ "$value" -gt 65535 ]; then
    echo "PORT and ERP_PORT must be integers from 1024 through 65535." >&2
    exit 2
  fi
done
if [ "$PORT" = "$ERP_PORT" ]; then echo "PORT and ERP_PORT must differ." >&2; exit 2; fi
command -v uv >/dev/null
WORKDIR="$(mktemp -d -t arc-erp-demo-XXXXXX)"
ERP_PID=""
cleanup() {
  if [ -n "$ERP_PID" ]; then kill "$ERP_PID" 2>/dev/null || true; fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
cd "$WORKDIR"
DEMO_KEY="sim-demo-key"

run_env() {
  env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" \
  ENVIRONMENT=local AUTH_MODE=testnet_tokens API_KEY="$DEMO_KEY" APPROVAL_TOKEN= \
  PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=frappe SCREENING_PROVIDER=fixture \
  DECISION_LAYER=heuristics SIGNER_BACKEND=env WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]' \
  MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=2000 SCREENING_MEDIUM_TIER_HANDLING=limit \
  FRAPPE_URL="http://127.0.0.1:$ERP_PORT" FRAPPE_API_KEY=sim-key FRAPPE_API_SECRET=sim-secret \
  FRAPPE_COMPANY="Arc Demo Inc" FRAPPE_PAID_FROM_ACCOUNT="USDC Wallet - ADI" \
  FRAPPE_PAID_TO_ACCOUNT="Creditors - ADI" FRAPPE_MODE_OF_PAYMENT="Arc Testnet" \
  FRAPPE_SETTLEMENT_CURRENCY=USDC FRAPPE_COMPANY_CURRENCY=USD FRAPPE_INVOICE_CURRENCY=USD \
  FRAPPE_SOURCE_EXCHANGE_RATE=1 FRAPPE_TARGET_EXCHANGE_RATE=1 \
  FRAPPE_FEE_ACCOUNT="Network Fees - ADI" FRAPPE_COST_CENTER="Main - ADI" FRAPPE_FEE_CURRENCY=USD \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" BACKUP_DIRECTORY="$WORKDIR/backups" \
  uv run --project "$ROOT" --frozen "$@"
}

printf 'Simulated ERPNext:  http://127.0.0.1:%s/   (a stand-in, not ERPNext)\n' "$ERP_PORT"
printf 'Arc Payables:       http://127.0.0.1:%s/console/\n' "$PORT"
printf 'In the console, open "Access keys" in the sidebar and enter the API access key: %s\n' "$DEMO_KEY"
printf 'Then import ACC-PINV-2026-00101 (clean) or ACC-PINV-2026-00102 (supplier wallet not yet verified).\n'
printf 'Payments are simulated and the chain is not touched. Disposable files: %s\n' "$WORKDIR"

run_env uvicorn arc_payables.sim_erpnext:app --host 127.0.0.1 --port "$ERP_PORT" --log-level warning &
ERP_PID=$!
run_env uvicorn arc_payables.api:app --host 127.0.0.1 --port "$PORT"
