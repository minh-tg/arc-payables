#!/usr/bin/env bash
# A disposable, loopback-only console for manual readiness checks. Never uses live credentials.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PROFILE="${1:-}"
SCREENING=fixture
RESERVE=2000
case "$PROFILE" in
  "") ;;
  --screening-unavailable) SCREENING=unavailable ;;
  --high-reserve) RESERVE=1000000000 ;;
  *) echo "Usage: bash script/manual-readiness-demo.sh [--screening-unavailable|--high-reserve]" >&2; exit 2 ;;
esac
if [ "$#" -gt 1 ]; then echo "Only one profile may be selected." >&2; exit 2; fi
PORT="${PORT:-8097}"
if [[ ! "$PORT" =~ ^[1-9][0-9]{3,4}$ ]] || [ "$PORT" -lt 1024 ] || [ "$PORT" -gt 65535 ]; then
  echo "PORT must be an integer from 1024 through 65535 (without leading zeros)." >&2
  exit 2
fi
command -v uv >/dev/null
WORKDIR="$(mktemp -d -t arc-manual-readiness-XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
# Settings reads .env from the current directory. A fresh directory plus a clean environment
# prevents project .env and shell credentials, signing backends or webhooks from leaking in.
cd "$WORKDIR"
printf 'Manual readiness: mock payments/accounting, %s screening, heuristics (no LLM).\n' "$SCREENING"
printf 'Open http://127.0.0.1:%s/console/ in a fresh/private browser window.\n' "$PORT"
printf 'Disposable files: %s (deleted on exit; save redacted evidence before Ctrl-C).\n' "$WORKDIR"
demo_command() {
  env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" \
  ENVIRONMENT=local AUTH_MODE=demo PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock \
  SCREENING_PROVIDER="$SCREENING" DECISION_LAYER=heuristics SIGNER_BACKEND=env \
  API_KEY= APPROVAL_TOKEN= WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]' \
  MIN_RESERVE_USDC="$RESERVE" MAX_INVOICE_USDC=1000 \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" BACKUP_DIRECTORY="$WORKDIR/backups" \
  uv run --project "$ROOT" --frozen "$@"
}
# /demo/start intentionally requires the default policy and fixture screening. Non-default
# failure-injection profiles seed explicit synthetic fixtures locally instead of weakening it.
if [ -n "$PROFILE" ]; then demo_command arc-payables-seed; fi
demo_command uvicorn arc_payables.api:app --host 127.0.0.1 --port "$PORT"
