#!/usr/bin/env bash
# The ERPNext use case in four checked stages. Nothing here creates a site, an API user or a
# payment on its own: each stage that writes needs an explicit flag, and the one that moves funds
# is the existing arc-payables-live-run with its own --confirm.
#
#   bash script/erpnext-use-case.sh check
#   bash script/erpnext-use-case.sh provision [--apply]
#   bash script/erpnext-use-case.sh settle <local-invoice-id> [--confirm]
#   bash script/erpnext-use-case.sh verify <tx-hash> [purchase-invoice-name]
#
# Credentials come from the environment (FRAPPE_URL, FRAPPE_API_KEY, FRAPPE_API_SECRET) and are
# never printed or passed on a command line. See docs/erpnext-use-case.md for the full order.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
command -v uv >/dev/null || { echo "uv is required." >&2; exit 2; }

usage() {
  sed -n '6,9p' "$0" | sed 's/^# \{0,1\}//' >&2
  exit 2
}

require_env() {
  local missing=0 name
  for name in "$@"; do
    if [ -z "${!name:-}" ]; then echo "Missing environment variable: $name" >&2; missing=1; fi
  done
  [ "$missing" -eq 0 ] || exit 2
}

stage_check() {
  local url="${FRAPPE_URL:-http://127.0.0.1:8080}"
  echo "1. ERPNext reachable at ${url%%\?*}"
  local pong
  pong="$(curl --silent --show-error --max-time 10 "$url/api/method/ping" || true)"
  case "$pong" in
    *pong*) echo "   OK    ping answered" ;;
    *) echo "   FAIL  no pong from $url; start the sandbox (deploy/erpnext/README.md steps 1 to 4)" >&2; exit 1 ;;
  esac
  require_env FRAPPE_API_KEY FRAPPE_API_SECRET
  echo "2. API user is not Administrator"
  local user
  # The header goes in through a config stream so the token never appears in a process listing.
  user="$(curl --silent --show-error --max-time 10 \
    --config <(printf 'header = "Authorization: token %s:%s"\n' "$FRAPPE_API_KEY" "$FRAPPE_API_SECRET") \
    "$url/api/method/frappe.auth.get_logged_user" | uv run python -c \
    'import json,sys; print(json.load(sys.stdin).get("message",""))' 2>/dev/null || true)"
  if [ -z "$user" ]; then echo "   FAIL  credentials were not accepted" >&2; exit 1; fi
  if [ "$(printf '%s' "$user" | tr '[:upper:]' '[:lower:]')" = "administrator" ]; then
    echo "   FAIL  Administrator credentials are refused by the connector" >&2; exit 1
  fi
  echo "   OK    authenticated as $user"
  echo "3. Accounting mapping is complete"
  FRAPPE_URL="$url" uv run python - <<'PY'
from arc_payables.settings import get_settings
settings = get_settings()
if not settings.frappe_accounting_ready:
    raise SystemExit("   FAIL  FRAPPE_COMPANY, the three accounts, mode of payment, currencies, rates and cost centre must all be set")
print("   OK    every writeback mapping value is set (account checks run against ERPNext at payment time)")
PY
}

stage_provision() {
  local mode="--dry-run"
  [ "${1:-}" = "--apply" ] && mode="--apply"
  require_env FRAPPE_URL FRAPPE_API_KEY FRAPPE_API_SECRET DEMO_SUPPLIER_WALLET DEMO_POSTING_DATE
  echo "Provisioning ($mode) with the operator credentials; this creates no payment."
  uv run arc-payables-erpnext-bootstrap \
    --company "${FRAPPE_COMPANY:-Arc Demo Inc}" \
    --wallet "$DEMO_SUPPLIER_WALLET" \
    --posting-date "$DEMO_POSTING_DATE" \
    --invoice-reference "${DEMO_INVOICE_REFERENCE:-ARC-PAYABLES-DEMO-001}" \
    "$mode"
  [ "$mode" = "--apply" ] || echo "Preview only. Re-run with --apply after reading it."
}

stage_settle() {
  local invoice="${1:-}"; shift || true
  [ -n "$invoice" ] || usage
  local flag=""
  [ "${1:-}" = "--confirm" ] && flag="--confirm"
  if [ -n "$flag" ]; then
    echo "This sends a real Arc Testnet payment from your configured wallet."
  else
    echo "Preflight only; nothing is signed. Add --confirm to send."
  fi
  # shellcheck disable=SC2086
  uv run arc-payables-live-run --invoice "$invoice" $flag
}

stage_verify() {
  local tx="${1:-}"
  [ -n "$tx" ] || usage
  require_env FRAPPE_URL FRAPPE_API_KEY FRAPPE_API_SECRET
  if [ -n "${2:-}" ]; then
    uv run arc-payables-erp-verify --tx "$tx" --invoice "$2"
  else
    uv run arc-payables-erp-verify --tx "$tx"
  fi
}

stage="${1:-}"; shift || true
case "$stage" in
  check) stage_check ;;
  provision) stage_provision "$@" ;;
  settle) stage_settle "$@" ;;
  verify) stage_verify "$@" ;;
  *) usage ;;
esac
