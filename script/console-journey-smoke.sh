#!/usr/bin/env bash
# Exercise the first-payment journey against a fresh, explicitly mock-only database.
# Requires uv, chromium and agent-browser. No credentials, external integration or live payment.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT="${PORT:-8098}"
WORKDIR="$(mktemp -d)"
SESSION="tameion-journey-$$"
SERVER_PID=""
BROWSER=(agent-browser --session "$SESSION" --executable-path "$(command -v chromium)")
cleanup() {
  "${BROWSER[@]}" close >/dev/null 2>&1 || true
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT
command -v agent-browser >/dev/null
env PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics \
  API_KEY= APPROVAL_TOKEN= MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=1000 CRITICAL_SUPPLIER_IDS='[]' \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" uv run uvicorn arc_payables.api:app --port "$PORT" >"$WORKDIR/api.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null
browser() { "${BROWSER[@]}" "$@"; }
check() { browser eval "$1"; }
ready() { browser wait --fn "document.querySelector('#view').getAttribute('aria-busy') === 'false'"; }
assert_no_payment() {
  curl -fsS "http://127.0.0.1:$PORT/payments" | python3 -c 'import json,sys; assert json.load(sys.stdin)["count"] == 0'
}
browser open "http://127.0.0.1:$PORT/console/"
ready
check "if(!document.querySelector('#environment-label').textContent.includes('Demo')) throw Error('Missing demo label');"
assert_no_payment
browser click 'button[data-action=start-demo]'
browser wait --text 'Examples loaded'
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-suspicious"
ready
browser click 'button[data-action=evaluate]'
browser wait --text 'Correct the conflicting or missing evidence'
check "if(document.querySelector('[data-action=review-payment]') || [...document.querySelectorAll('button')].some(b=>b.textContent.includes('Record approval'))) throw Error('Blocking evidence offered approval/payment');"
assert_no_payment
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
browser click 'button[data-action=evaluate]'
browser wait 'button[data-action=review-payment]'
# Metadata failure must not create a dialog or a payment.
check "window.originalFetch=window.fetch; window.fetch=async(path,options)=>path==='/setup'?new Response('{}',{status:200}):window.originalFetch(path,options);"
browser click 'button[data-action=review-payment]'
browser wait --text 'environment_unknown'
check "if(document.querySelector('dialog')) throw Error('Unknown mode offered confirmation'); window.fetch=window.originalFetch;"
assert_no_payment
browser click 'button[data-action=review-payment]'
browser wait 'dialog[open]'
check "const text=document.querySelector('dialog').textContent; if(!text.includes('Confirm simulated payment') || !text.includes('250 USDC') || !text.includes('0x1111111111111111111111111111111111111111')) throw Error('Confirmation omitted exact payment or simulation');"
browser press Escape
browser wait --fn "!document.querySelector('dialog') && !document.querySelector('[data-action=review-payment]').disabled"
assert_no_payment
browser click 'button[data-action=review-payment]'
browser wait 'dialog[open]'
browser click 'button[data-action=confirm-payment]'
browser wait --text 'ERP_RECORDED'
check "if(document.querySelector('[data-action=review-payment]')) throw Error('Duplicate payment control'); if(!document.querySelector('main').textContent.includes('Settlement & accounting entries')) throw Error('Ledger entries missing');"
browser click 'button[data-action=verify-audit]'
browser wait --text 'Intact ·'
browser click 'a[href="#/payments/demo-invoice-legitimate"]'
browser wait --text 'Ledger writeback'
# A delayed old route must not replace a newer one.
check "window.originalFetch=window.fetch; window.fetch=async(path,options)=>{if(String(path).startsWith('/forecast')) await new Promise(r=>setTimeout(r,500)); return window.originalFetch(path,options);}; location.hash='#/overview'; setTimeout(()=>location.hash='#/queue',20);"
browser wait --text 'Pay in this order'
browser wait 700
check "if(document.querySelector('#view').textContent.includes('Know what can move')) throw Error('Stale route overwrote queue'); window.fetch=window.originalFetch;"
# Populate real durable worker advice, still using only local mock providers. Opening history
# must not start payments, and expanded long evidence identifiers must fit a phone viewport.
env PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics \
  API_KEY= APPROVAL_TOKEN= MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=1000 CRITICAL_SUPPLIER_IDS='[]' \
  DATABASE_PATH="$WORKDIR/demo.sqlite3" uv run arc-payables-worker --once --autopay --intake >/dev/null
curl -fsS "http://127.0.0.1:$PORT/plans" | python3 -c 'import json,sys; rows=json.load(sys.stdin)["plans"]; assert any(r["status"]=="executed" for r in rows)'
# Crossing API/worker process boundaries must not silently invalidate the demo audit anchor.
curl -fsS "http://127.0.0.1:$PORT/audit/verify" | python3 -c 'import json,sys; report=json.load(sys.stdin); assert report["ok"], report; assert report["signed"] > 0; assert report["signature_anchor"]=="configured_signer"'
for view in overview attention queue payments audit worker treasury setup invoice/demo-invoice-legitimate; do
  browser open "http://127.0.0.1:$PORT/console/#/$view"
  ready
  if [ "$view" = queue ]; then
    check "if(!document.querySelector('#view').textContent.includes('Payment attempted')) throw Error('Recorded plan history missing'); document.querySelectorAll('#view details').forEach(d=>d.open=true);"
  fi
  for width in 1440 390 320; do
    browser set viewport "$width" 900
    check "if(document.documentElement.scrollWidth > innerWidth + 1) throw Error('Horizontal overflow on $view at $width'); if(document.querySelector('#view').textContent.includes('Request failed')) throw Error('View failed: $view');"
  done
done
browser click '#menu-toggle'
check "if(document.querySelector('#menu-toggle').getAttribute('aria-expanded') !== 'true' || document.activeElement.id !== 'menu-close') throw Error('Mobile menu inaccessible');"
browser press Escape
check "if(document.querySelector('#navigation').hasAttribute('data-open') || document.activeElement.id !== 'menu-toggle') throw Error('Menu did not restore focus');"
browser set media reduced-motion
check "if(getComputedStyle(document.querySelector('.log-cursor') || document.querySelector('.agent-dot')).animationName !== 'none') throw Error('Reduced motion ignored');"
echo 'Golden path, blocked evidence, unknown mode, cancellation, route races and responsive layouts passed.'
