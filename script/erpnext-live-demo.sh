#!/usr/bin/env bash
# The console against the real ERPNext sandbox with a REAL payment on Arc Testnet (test money).
#
#   ERPNext sandbox (127.0.0.1:8080)  <--REST-->  Arc Payables console (127.0.0.1:8097)
#                                                   --> Circle wallet --> PaymentGuard on Arc Testnet
#
# This spends test USDC and gas from the Circle wallet when a payment is confirmed in the console.
# Nothing is sent by starting it. Use a tiny bill: the guard's caps are the real limit.
#
# Two private, git-ignored files hold the credentials (mode 600, never printed):
#   .secret/erpnext-sandbox.env  FRAPPE_* (script/erpnext-sandbox-setup.sh writes it)
#   .secret/arc-live.env         CIRCLE_* and PERMIT_SIGNING_* for the wallet and the guard
# Only those names are read from them, the ERPNext URL must be on this machine, and nothing else
# from your environment reaches the console.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8097}"
ERP_ENV_FILE="${ERP_ENV_FILE:-$ROOT/.secret/erpnext-sandbox.env}"
LIVE_ENV_FILE="${LIVE_ENV_FILE:-$ROOT/.secret/arc-live.env}"
CONSOLE_KEY="local-console-key"
if [[ ! "$PORT" =~ ^[1-9][0-9]{3,4}$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
for file in "$ERP_ENV_FILE" "$LIVE_ENV_FILE"; do
  [ -r "$file" ] || { echo "Missing credentials file: $file" >&2; exit 2; }
done
command -v uv >/dev/null

PAIRS=()
FRAPPE_URL_VALUE=""
read_pairs() {
  local file="$1" pattern="$2" line name value
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^($pattern)= ]]; then
      name="${line%%=*}"; value="${line#*=}"
      if [[ "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
      PAIRS+=("$name=$value")
      if [ "$name" = "FRAPPE_URL" ]; then FRAPPE_URL_VALUE="$value"; fi
    fi
  done < "$file"
  return 0
}
read_pairs "$ERP_ENV_FILE" 'FRAPPE_[A-Z0-9_]+'
read_pairs "$LIVE_ENV_FILE" 'CIRCLE_[A-Z0-9_]+|PERMIT_SIGNING_(PRIVATE_KEY|ADDRESS)'
if [[ ! "$FRAPPE_URL_VALUE" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]]; then
  echo "FRAPPE_URL must be http://127.0.0.1 or http://localhost. This demo never talks to a remote ERPNext." >&2
  exit 2
fi
if ! curl --fail --silent --max-time 5 "$FRAPPE_URL_VALUE/api/method/ping" >/dev/null; then
  echo "ERPNext does not answer at $FRAPPE_URL_VALUE. Start the sandbox first (deploy/erpnext/README.md)." >&2
  exit 1
fi

WORKDIR="$(mktemp -d -t arc-live-demo-XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"
printf 'ERPNext (real sandbox): %s\n' "$FRAPPE_URL_VALUE"
printf 'Arc Payables:           http://127.0.0.1:%s/console/\n' "$PORT"
printf 'Console key (Access keys in the sidebar): %s\n' "$CONSOLE_KEY"
printf 'LIVE: confirming a payment sends test USDC on Arc Testnet through the Circle wallet and the guard.\n'
printf 'Disposable files: %s\n' "$WORKDIR"
env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" \
  ENVIRONMENT=local AUTH_MODE=testnet_tokens API_KEY="$CONSOLE_KEY" APPROVAL_TOKEN= \
  PAYMENT_PROVIDER=circle ACCOUNTING_PROVIDER=frappe SCREENING_PROVIDER=fixture \
  DECISION_LAYER=heuristics SIGNER_BACKEND=env WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]' \
  MIN_RESERVE_USDC=0 MAX_INVOICE_USDC=1 SCREENING_MEDIUM_TIER_HANDLING=limit \
  "${PAIRS[@]}" \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" BACKUP_DIRECTORY="$WORKDIR/backups" \
  uv run --project "$ROOT" --frozen uvicorn arc_payables.api:app --host 127.0.0.1 --port "$PORT"
