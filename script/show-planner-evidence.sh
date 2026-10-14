#!/usr/bin/env bash
# Print the saved live-model run in a screen-sized summary. Reads a file; calls nothing.
set -euo pipefail
cd "$(dirname "$0")/.."
jq -r '
  "model: \(.model)   funds moved: \(.funds_moved)   run: \(.checked_at_utc[0:16])Z",
  "",
  "model asked about a missing fact?   \(.cases.missing_evidence_never_reaches_the_model.deliberations | length) calls",
  "limit trade-off:                    \(.cases.trade_off_is_deliberated.advisory_action) - \(.cases.trade_off_is_deliberated.rationale[0:150])...",
  "hostile invoice text -> model said \(.cases.prompt_injection_cannot_move_the_payment.advisory_action):",
  "  \(.cases.prompt_injection_cannot_move_the_payment.rationale | capture("(?<s>The untrusted.*)").s)",
  "final decision after policy:        \(.cases.prompt_injection_cannot_move_the_payment.final_action)",
  "",
  "queue order, rules:  \(.cases.model_orders_the_queue_within_the_balance.baseline_order | join(" > "))",
  "queue order, model:  \(.cases.model_orders_the_queue_within_the_balance.model_order | join(" > "))",
  "spend: \(.cases.model_orders_the_queue_within_the_balance.planned_spend_usdc) of \(.cases.model_orders_the_queue_within_the_balance.spendable_usdc) USDC spendable"
' docs/evidence/decision-layer/live-planner-report.json
