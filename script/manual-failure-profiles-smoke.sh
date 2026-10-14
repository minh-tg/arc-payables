#!/usr/bin/env bash
# M11/M12: real browser, explicitly synthetic fixtures; never approve or submit a payment.
set -euo pipefail
PROFILE="${1:-}"
if [ "$#" -ne 1 ]; then
  echo 'Usage: bash script/manual-failure-profiles-smoke.sh --screening-unavailable|--high-reserve' >&2
  exit 2
fi
case "$PROFILE" in
  --screening-unavailable) SCREENING=unavailable; RESERVE=2000; CHECK_CODE=screening_unavailable ;;
  --high-reserve) SCREENING=fixture; RESERVE=1000000000; CHECK_CODE=cash_reserve ;;
  *) echo 'Only the two explicit mock failure profiles are accepted.' >&2; exit 2 ;;
esac
source "$(dirname "$0")/smoke-common.sh"
PORT="${PORT:-8120}"
smoke_require_free_port "$PORT"
# uv runs env after the common clean environment has been applied. Only these two bounded
# failure-injection values differ; no inherited credentials or project dotenv are reintroduced.
mock_command env SCREENING_PROVIDER="$SCREENING" MIN_RESERVE_USDC="$RESERVE" arc-payables-seed >/dev/null
mock_server env SCREENING_PROVIDER="$SCREENING" MIN_RESERVE_USDC="$RESERVE" \
  uvicorn arc_payables.api:app --host 127.0.0.1 --port "$PORT" >"$WORKDIR/api.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 40); do
  kill -0 "$SERVER_PID" 2>/dev/null || { echo 'Our isolated server exited before readiness.' >&2; exit 3; }
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.25
done
curl -fsS "http://127.0.0.1:$PORT/setup" | python3 -c '
import json,sys
deployment=json.load(sys.stdin)["deployment"]
assert deployment["mode"]=="demo"
assert deployment["payment_provider"]==deployment["accounting_provider"]=="mock"
assert deployment["screening_provider"]==sys.argv[1]
' "$SCREENING"
assert_unapproved_unpaid() {
  curl -fsS "http://127.0.0.1:$PORT/payments" | python3 -c 'import json,sys; assert json.load(sys.stdin)["count"]==0'
  curl -fsS "http://127.0.0.1:$PORT/invoices/demo-invoice-legitimate" | python3 -c '
import json,sys
detail=json.load(sys.stdin)
assert not detail["payment"] and not detail["approvals"]
'
}
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
assert_unapproved_unpaid
browser click 'button[data-action=evaluate]'
browser wait --text 'A human must acknowledge'
check "const detail=await(await fetch('/invoices/demo-invoice-legitimate')).json(); const failed=detail.decision.policy_checks.find(c=>c.code==='$CHECK_CODE'); if(detail.state!=='ESCALATED'||detail.decision.action!=='ESCALATE'||!failed||failed.passed||!failed.overridable||!failed.requires_human) throw Error('Expected reviewable failure missing: $CHECK_CODE'); if(document.querySelector('[data-action=review-payment]')||document.querySelector('dialog')) throw Error('Unapproved profile offered payment');"
check "const app=await import('/console/app.js'); const label=app.humanizeCode('$CHECK_CODE'); const row=[...document.querySelectorAll('#view tr')].find(r=>r.querySelector('td')?.textContent.trim()===label); if(!row||!row.children[1].textContent.includes('Failed')||!row.children[2].textContent.includes('Human review permitted')) throw Error('Failure/review evidence missing from UI'); if(!document.querySelector('[data-action=record-approval]')) throw Error('Reviewable exception not explained');"
assert_unapproved_unpaid
# Reload and inspect the plan only; do not tick acknowledgements, enter tokens or record approval.
browser reload
ready
check "if(document.querySelector('[data-action=review-payment]')) throw Error('Reload bypassed unapproved failure');"
browser open "http://127.0.0.1:$PORT/console/#/queue"
ready
check "const plan=await(await fetch('/plan')).json(); if(plan.ordered.length || Number(plan.planned_spend_usdc)!==0) throw Error('Unapproved plan allocated a payment');"
if [ "$PROFILE" = --high-reserve ]; then
  check "const plan=await(await fetch('/plan')).json(); if(Number(plan.reserve_floor_usdc)!==1000000000||Number(plan.spendable_usdc)!==0) throw Error('Reserve allocation did not fail closed'); const terms=[...document.querySelectorAll('#view dt')]; for(const name of ['Spendable','Planned spend']) {const term=terms.find(t=>t.textContent===name); if(!term||parseFloat(term.nextElementSibling.textContent)!==0) throw Error('UI allocated below reserve: '+name);}"
fi
for width in 1440 390 320; do
  browser set viewport "$width" 900
  check "if(document.documentElement.scrollWidth>innerWidth+1||document.querySelector('#view').textContent.includes('Request failed')) throw Error('Failure-profile queue layout broken at $width');"
done
browser open "http://127.0.0.1:$PORT/console/#/invoice/demo-invoice-legitimate"
ready
for width in 1440 390 320; do
  browser set viewport "$width" 900
  check "if(document.documentElement.scrollWidth>innerWidth+1||document.querySelector('[data-action=review-payment]')) throw Error('Failure-profile review unsafe at $width');"
done
assert_unapproved_unpaid
echo "PASS $PROFILE: $CHECK_CODE escalated; visible review, no payment control, no allocation, no approval/payment, responsive layouts."
