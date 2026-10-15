#!/usr/bin/env bash
# One worker pass with autopay against mock providers, in a temporary directory.
#
# For a recording: seeds disposable demo data, runs the existing worker once with intake and
# autopay, prints a short summary, and deletes everything it wrote. It refuses to run when the
# shell already carries live provider settings or credentials, so a mocked pass cannot be read as
# a live one.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# A value in one of these names selects a live path or holds a live credential. The message names
# the variable; a credential's value is never printed.
PROVIDER_SELECTORS=(PAYMENT_PROVIDER ACCOUNTING_PROVIDER SCREENING_PROVIDER)
LIVE_CREDENTIALS=(
  CIRCLE_API_KEY CIRCLE_ENTITY_SECRET CIRCLE_WALLET_ID CIRCLE_GUARD_ADDRESS
  FRAPPE_URL FRAPPE_API_KEY FRAPPE_API_SECRET
  PLANNER_BASE_URL PLANNER_MODEL PLANNER_API_KEY
  OPENSANCTIONS_API_KEY ALERT_WEBHOOK_URL
  PERMIT_SIGNING_PRIVATE_KEY LOCAL_PAYMENT_PRIVATE_KEY PAYMENT_GUARD_PAUSER_KEY
)

header() { printf '%s  %s\n' "$1" "$2"; }
field() { printf '%-20s%s\n' "$1:" "$2"; }

# D8: a refused configuration exits 2, because the run never started.
refuse() {
  header FAIL "live provider settings were found and refused"
  field Mode "not started"
  field Reason "$1"
  field Storage "temporary directory, removed on exit"
  field "Funds moved" "none"
  exit 2
}

for name in "${PROVIDER_SELECTORS[@]}"; do
  value="${!name:-}"
  [ -z "$value" ] && continue
  case "$name" in
    PAYMENT_PROVIDER|ACCOUNTING_PROVIDER) [ "$value" = mock ] && continue ;;
    SCREENING_PROVIDER) [ "$value" = fixture ] && continue ;;
  esac
  refuse "$name=$value is set; unset it and run again"
done
for name in "${LIVE_CREDENTIALS[@]}"; do
  [ -n "${!name:-}" ] && refuse "$name is set; unset it and run again"
done

WORKDIR="$(mktemp -d -t arc-unattended-loop-XXXXXX)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
# Run from the temporary directory so Settings cannot read a project .env or any other file here.
cd "$WORKDIR"

# env -i: the mock configuration is stated here, so an inherited variable cannot select a live
# path or stay visible to the worker even when a check above was missed.
demo_env() {
  env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" \
    ENVIRONMENT=local AUTH_MODE=demo PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock \
    SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics SIGNER_BACKEND=env \
    API_KEY= APPROVAL_TOKEN= WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]' \
    MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=1000 \
    CIRCLE_API_KEY= CIRCLE_ENTITY_SECRET= CIRCLE_WALLET_ID= CIRCLE_GUARD_ADDRESS= \
    FRAPPE_URL= FRAPPE_API_KEY= FRAPPE_API_SECRET= \
    PLANNER_BASE_URL= PLANNER_MODEL= PLANNER_API_KEY= \
    OPENSANCTIONS_API_KEY= ALERT_WEBHOOK_URL= \
    PERMIT_SIGNING_PRIVATE_KEY= LOCAL_PAYMENT_PRIVATE_KEY= PAYMENT_GUARD_PAUSER_KEY= \
    DATABASE_PATH="$WORKDIR/demo.sqlite3" BACKUP_DIRECTORY="$WORKDIR/backups" \
    uv run --project "$ROOT" --frozen "$@"
}

failed_pass() {
  header FAIL "$1"
  field Mode "mock payment, mock accounting, fixture screening"
  field Storage "temporary directory, removed on exit"
  field "Pass outcome" "$2"
  field "Funds moved" "none, nothing is sent on chain"
  exit 1
}

if ! demo_env arc-payables-seed >/dev/null; then
  failed_pass "the disposable demo data could not be seeded" "not started"
fi

if ! summary="$(demo_env arc-payables-worker --once --autopay --intake)"; then
  failed_pass "the worker pass did not finish cleanly" "failed"
fi

case "$summary" in
  pass\ *) ;;
  *) failed_pass "the worker did not report a pass" "unknown" ;;
esac
outcome="${summary#pass }"; outcome="${outcome%%:*}"
steps="${summary#*: }"

payment="$(demo_env python - <<'PY'
import json
import os
import sqlite3
from decimal import Decimal

from arc_payables.domain import USDC_SCALE

with sqlite3.connect(f"file:{os.environ['DATABASE_PATH']}?mode=ro", uri=True) as connection:
    rows = connection.execute(
        "SELECT i.invoice_number, i.data_json FROM payments p JOIN invoices i ON i.id = p.invoice_id "
        "WHERE json_extract(p.record_json, '$.confirmation_status') = 'CONFIRMED' ORDER BY p.rowid"
    ).fetchall()
if rows:
    invoice_number, data = rows[-1]
    amount = Decimal(int(json.loads(data)['amount_units'])) / USDC_SCALE
    print(f"{len(rows)}|{invoice_number}|{amount:.2f}")
else:
    print("0||")
PY
)" || payment=""

count="${payment%%|*}"
rest="${payment#*|}"
invoice_number="${rest%%|*}"
amount="${rest#*|}"

if [ "$outcome" != "ok" ]; then
  failed_pass "worker pass finished $outcome" "$outcome"
fi

if ! [[ "$count" =~ ^[1-9][0-9]*$ ]]; then
  header INCONCLUSIVE "worker pass ok, no settlement was confirmed"
  field Mode "mock payment, mock accounting, fixture screening"
  field Storage "temporary directory, removed on exit"
  field "Pass outcome" "$outcome"
  field "Funds moved" "none, nothing is sent on chain"
  exit 3
fi

noun="payment"
[ "$count" != "1" ] && noun="payments"
header PASS "$count simulated $noun in one worker pass, mock providers"
field Mode "mock payment, mock accounting, fixture screening"
field Storage "temporary directory, removed on exit"
field "Pass outcome" "$outcome"
IFS=',' read -ra parts <<< "$steps"
for part in "${parts[@]}"; do
  part="${part#"${part%%[![:space:]]*}"}"
  if [[ "$part" =~ ^([A-Za-z_]+)=([0-9]+)/([0-9]+)(\ \(([0-9]+)\ failed\))?$ ]]; then
    failed="${BASH_REMATCH[5]:-}"
    if [ -n "$failed" ]; then
      printf '  %-18s%s acted (%s failed)\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}/${BASH_REMATCH[3]}" "$failed"
    else
      printf '  %-18s%s acted\n' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}/${BASH_REMATCH[3]}"
    fi
  fi
done
field "Simulated payment" "$invoice_number, $amount USDC"
field "Funds moved" "none, nothing is sent on chain"
exit 0
