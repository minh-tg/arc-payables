#!/usr/bin/env bash
# Put the ERPNext sandbox back to "one unpaid bill, supplier wallet not yet verified", for another run
# of script/erpnext-demo.sh. It un-verifies the demo supplier's wallet and seeds a fresh unpaid bill.
# It never pays anything and never touches a chain. Sandbox only: the URL must be on this machine.
#
#   bash script/erpnext-demo-reset.sh
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="${ERP_ENV_FILE:-$ROOT/.secret/erpnext-sandbox.env}"
COMPANY="Arc Demo Inc"
SUPPLIER="Arc Payables Demo Supplier - ADI"
WALLET="${DEMO_SUPPLIER_WALLET:-0x1111111111111111111111111111111111111111}"
[ -r "$ENV_FILE" ] || { echo "No sandbox credentials at $ENV_FILE (see deploy/erpnext/README.md)." >&2; exit 2; }
command -v uv >/dev/null

url=""; key=""; secret=""
while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    FRAPPE_URL=*) url="${line#FRAPPE_URL=}" ;;
    FRAPPE_API_KEY=*) key="${line#FRAPPE_API_KEY=}" ;;
    FRAPPE_API_SECRET=*) secret="${line#FRAPPE_API_SECRET=}" ;;
  esac
done < "$ENV_FILE"
if [[ ! "$url" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]]; then
  echo "FRAPPE_URL must be http://127.0.0.1 or http://localhost." >&2
  exit 2
fi
curl --fail --silent --max-time 5 "$url/api/method/ping" >/dev/null || { echo "ERPNext does not answer at $url." >&2; exit 1; }

encoded_supplier="${SUPPLIER// /%20}"
curl --fail --silent --max-time 15 -X PUT -H "Authorization: token $key:$secret" -H "Content-Type: application/json" \
  -d '{"custom_usdc_wallet_verified":0}' "$url/api/resource/Supplier/$encoded_supplier" >/dev/null
echo "Supplier wallet set to not verified."

reference="ARC-PAYABLES-DEMO-$(date +%H%M%S)"
posting_date="$(TZ=America/Moncton date +%F)"
set -a
FRAPPE_URL="$url"; FRAPPE_API_KEY="$key"; FRAPPE_API_SECRET="$secret"
set +a
uv run --project "$ROOT" --frozen arc-payables-erpnext-bootstrap \
  --company "$COMPANY" --wallet "$WALLET" --posting-date "$posting_date" \
  --invoice-reference "$reference" --apply | grep -E "^  purchase_invoice:|Sandbox provisioning complete"
echo "Import the Purchase Invoice named above in the console."
