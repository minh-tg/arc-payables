#!/usr/bin/env bash
# Real browser + cryptographic local IdP fixture. MFA is simulated; all funds are mock.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8100}"
IDP_PORT="${IDP_PORT:-8101}"
WORKDIR="$(mktemp -d)"
SESSION="tameion-oidc-$$"
SERVER_PID=""
IDP_PID=""
BROWSER=(agent-browser --session "$SESSION" --executable-path "$(command -v chromium)")
cleanup() {
  local status="$?"
  if [ "$status" -ne 0 ]; then "${BROWSER[@]}" snapshot >"$WORKDIR/browser.txt" 2>/dev/null || true; fi
  "${BROWSER[@]}" close >/dev/null 2>&1 || true
  [ -z "$SERVER_PID" ] || kill "$SERVER_PID" >/dev/null 2>&1 || true
  [ -z "$IDP_PID" ] || kill "$IDP_PID" >/dev/null 2>&1 || true
  [ -z "$SERVER_PID" ] || wait "$SERVER_PID" >/dev/null 2>&1 || true
  [ -z "$IDP_PID" ] || wait "$IDP_PID" >/dev/null 2>&1 || true
  if [ "$status" -eq 0 ]; then rm -rf "$WORKDIR"; else echo "OIDC fixture diagnostics retained at $WORKDIR" >&2; fi
}
trap cleanup EXIT
env OIDC_TEST_FIXTURE=1 OIDC_FIXTURE_ISSUER="http://127.0.0.1:$IDP_PORT" OIDC_FIXTURE_REDIRECT="http://127.0.0.1:$PORT/auth/callback" \
  uv run --frozen uvicorn oidc_fixture:fixture_app --factory --app-dir tests --port "$IDP_PORT" >"$WORKDIR/idp.log" 2>&1 &
IDP_PID=$!
env ENVIRONMENT=local AUTH_MODE=oidc OIDC_MFA_CLAIM=amr OIDC_MFA_VALUES='["mfa"]' OIDC_ISSUER="http://127.0.0.1:$IDP_PORT" OIDC_CLIENT_ID=tameion-test OIDC_CLIENT_SECRET= \
  OIDC_REDIRECT_URI="http://127.0.0.1:$PORT/auth/callback" OIDC_ALLOW_INSECURE_LOCALHOST=true \
  OIDC_SUBJECT_ROLES='{"reader":["reader"],"operator":["operator"],"approver":["approver"],"payer":["payer"],"admin":["admin"]}' \
  PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics \
  API_KEY= APPROVAL_TOKEN= MAX_INVOICE_USDC=100 MIN_RESERVE_USDC=2000 CRITICAL_SUPPLIER_IDS='[]' DATABASE_PATH="$WORKDIR/identity.sqlite3" \
  uv run --frozen uvicorn arc_payables.api:app --port "$PORT" >"$WORKDIR/api.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && curl -fsS "http://127.0.0.1:$IDP_PORT/jwks" >/dev/null 2>&1; then break; fi
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null
browser() { "${BROWSER[@]}" "$@"; }
check() { browser eval "(async()=>{$1;return null;})()"; }
ready() { browser wait --fn "document.querySelector('#view .card') && !document.querySelector('#view .loading')"; }
login() {
  browser open "http://127.0.0.1:$PORT/console/"
  browser wait '#identity-login'
  browser click '#identity-login'
  browser wait "a[data-subject=$1]"
  browser click "a[data-subject=$1]"
  browser wait --text "Signed in: $1"
  ready
}
logout() { browser set viewport 1440 900; browser click '#identity-logout'; browser wait '#identity-login'; }
login reader
check "if(!document.querySelector('#shared-credentials').hidden) throw Error('Shared tokens exposed in OIDC mode'); if(document.cookie.includes('arc_payables_session')) throw Error('Session not HttpOnly');"
check "const app=await import('/console/app.js'); try {await app.api('/demo/start',{method:'POST'});throw Error('Reader mutated demo');} catch(e) {if(e.status!==403||e.detail?.code!=='forbidden') throw e;}"
logout
login operator
browser click 'button[data-action=start-demo]'
browser wait --text 'Examples loaded'
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-suspicious"
ready
browser click 'button[data-action=evaluate]'
browser wait --text 'Correct the conflicting or missing evidence'
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
browser click 'button[data-action=evaluate]'
browser wait --text 'A human must acknowledge'
check "if(document.querySelector('#approval-note')||document.querySelector('[data-action=review-payment]')) throw Error('Operator offered checker/payer controls');"
logout
login approver
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
browser wait '#approval-note'
check "if(document.querySelector('#approval-reviewer')) throw Error('Client can invent reviewer');document.querySelectorAll('.checks input').forEach(e=>e.checked=true);"
browser fill '#approval-note' 'Reviewed independent evidence in the local identity fixture.'
browser click '[data-action=record-approval]'
browser wait --text 'Checks passed'
check "if(document.querySelector('[data-action=review-payment]')) throw Error('Approver offered payment'); const detail=await(await fetch('/invoices/demo-invoice-legitimate')).json();if(detail.approvals.at(-1).reviewer_identity.subject!=='approver')throw Error('Lost reviewer attribution');"
logout
login payer
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
browser wait '[data-action=review-payment]'
check "if(document.querySelector('[data-action=evaluate]')||document.querySelector('#approval-note')) throw Error('Payer offered operator/checker controls');"
browser click '[data-action=review-payment]'
browser wait 'dialog[open]'
browser click '[data-action=confirm-payment]'
browser wait --text 'ERP_RECORDED'
browser click '[data-action=verify-audit]'
browser wait --text 'Intact ·'
for width in 1440 390 320; do
  browser set viewport "$width" 900
  check "if(document.documentElement.scrollWidth>innerWidth+1) throw Error('OIDC console overflow at $width');"
done
logout
login admin
check "const app=await import('/console/app.js');await app.api('/auth/revoke',{method:'POST',body:{subject:'approver'}});"
logout
browser open "http://127.0.0.1:$PORT/auth/login"
browser wait 'a[data-subject=approver]'
browser click 'a[data-subject=approver]'
browser wait --text 'forbidden'
echo 'OIDC fixture: PKCE sign-in, HttpOnly sessions, role separation, attributed approval, mock payment, audit verification, logout, revocation and responsive layouts passed.'
