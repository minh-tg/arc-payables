#!/usr/bin/env bash
# The console beside a real, disposable ERPNext sandbox, with payments simulated.
#
#   ERPNext sandbox (127.0.0.1:8080, containers)  <--REST-->  Arc Payables console (127.0.0.1:8097)
#                                                               --> simulated payment (no chain)
#
# Bills are read from ERPNext, decided here, "paid" by the mock payment provider, and the Payment
# Entry is written back to ERPNext. The ERPNext data is real sandbox data; the payment is not.
#
# Setup (once): deploy/erpnext/README.md. The credentials for the sandbox's integration user live in
# a private file that is never committed (default .secret/erpnext-sandbox.env, mode 600):
#   FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET and the FRAPPE_* account mapping the bootstrap prints.
# This script reads only FRAPPE_* names from that file, refuses any URL that is not on this machine,
# and passes nothing else from your environment to the console.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PORT="${PORT:-8097}"
ENV_FILE="${ERP_ENV_FILE:-$ROOT/.secret/erpnext-sandbox.env}"
CONSOLE_KEY="local-console-key"
if [[ ! "$PORT" =~ ^[1-9][0-9]{3,4}$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "PORT must be an integer from 1024 through 65535." >&2
  exit 2
fi
[ -r "$ENV_FILE" ] || { echo "No sandbox credentials at $ENV_FILE (see deploy/erpnext/README.md)." >&2; exit 2; }
command -v uv >/dev/null

FRAPPE_PAIRS=()
FRAPPE_URL_VALUE=""
while IFS= read -r line || [ -n "$line" ]; do
  if [[ "$line" =~ ^(FRAPPE_[A-Z0-9_]+)=(.*)$ ]]; then
    name="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    if [[ "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    FRAPPE_PAIRS+=("$name=$value")
    [ "$name" = "FRAPPE_URL" ] && FRAPPE_URL_VALUE="$value"
  fi
done < "$ENV_FILE"
if [[ ! "$FRAPPE_URL_VALUE" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]]; then
  echo "FRAPPE_URL must be http://127.0.0.1 or http://localhost. This demo never talks to a remote ERPNext." >&2
  exit 2
fi
if ! curl --fail --silent --max-time 5 "$FRAPPE_URL_VALUE/api/method/ping" >/dev/null; then
  echo "ERPNext does not answer at $FRAPPE_URL_VALUE. Start the sandbox first (deploy/erpnext/README.md)." >&2
  exit 1
fi

WORKDIR="$(mktemp -d -t arc-erpnext-demo-XXXXXX)"
trap 'rm -rf "$WORKDIR"' EXIT
cd "$WORKDIR"
printf 'ERPNext (real sandbox): %s\n' "$FRAPPE_URL_VALUE"
printf 'Arc Payables:           http://127.0.0.1:%s/console/\n' "$PORT"
printf 'In the console open "Access keys" in the sidebar and enter the API access key: %s\n' "$CONSOLE_KEY"
printf 'Import the bill that script/erpnext-demo-reset.sh printed. Payments are simulated and the chain is not touched.\n'
printf 'Disposable files: %s\n' "$WORKDIR"
env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" \
  ENVIRONMENT=local AUTH_MODE=testnet_tokens API_KEY="$CONSOLE_KEY" APPROVAL_TOKEN= \
  PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=frappe SCREENING_PROVIDER=fixture \
  DECISION_LAYER=heuristics SIGNER_BACKEND=env WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]' \
  MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=2000 SCREENING_MEDIUM_TIER_HANDLING=limit \
  "${FRAPPE_PAIRS[@]}" \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" BACKUP_DIRECTORY="$WORKDIR/backups" \
  uv run --project "$ROOT" --frozen uvicorn arc_payables.api:app --host 127.0.0.1 --port "$PORT"
