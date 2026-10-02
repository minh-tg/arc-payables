#!/usr/bin/env bash
# Render every console view in a real browser and fail if one of them throws.
#
# The Python suite checks that the console's modules parse and that their imports resolve, which is
# all it can see. It cannot see a view that parses, loads, and then dies on the shape of an API
# response: a wrong destructure is invisible to every server-side test, and one shipped that way for
# a week. This renders each view with the seeded demo data and looks for the router's error panel.
#
# It also checks the guided view, which no server-side test can reach either: every term it draws on
# a page has to have a definition behind it, because a term with no definition renders an empty
# popover and throws nothing at all.
#
# Needs chromium on PATH. Not part of CI, because that suite is hermetic and browser-free by design.
#
#   ./script/console-smoke.sh
set -euo pipefail

cd "$(dirname "$0")/.."

PORT="${PORT:-8096}"
VIEWS=(attention queue payments worker treasury setup)
WORKDIR="$(mktemp -d)"
DB="$WORKDIR/demo.sqlite3"
LOG="$WORKDIR/api.log"
SERVER_PID=""

cleanup() {
  if [ -n "$SERVER_PID" ]; then kill "$SERVER_PID" 2>/dev/null || true; fi
  rm -rf "$WORKDIR"
}
trap cleanup EXIT

if ! command -v chromium >/dev/null; then
  echo "chromium is not on PATH; nothing to render with" >&2
  exit 2
fi

# The fixture is pinned to the demo adapters. A developer with live credentials exported would
# otherwise have this script, which only exists to look at screens, move real testnet money.
MOCK="PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock"

DATABASE_PATH="$DB" uv run arc-payables-seed >/dev/null
# Drive one pass before rendering. An empty table cannot show a wrong destructure, which is the
# failure this script exists to catch, and most of the console is empty straight after seeding: no
# decisions, no payments, no worker steps. One pass fills the settlement log, the invoice's policy
# checks and the worker's six steps, so the checks below have rows to look at.
if ! env $MOCK DATABASE_PATH="$DB" uv run arc-payables-worker --once --autopay --intake >/dev/null; then
  echo "the demo worker pass failed, so the views would render empty" >&2
  exit 3
fi

env $MOCK DATABASE_PATH="$DB" uv run uvicorn arc_payables.api:app --port "$PORT" >"$LOG" 2>&1 &
SERVER_PID=$!

for _ in $(seq 1 40); do
  curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null

# The evaluated invoice, not the first one: the first is usually still RECEIVED, with no decision,
# no policy checks and therefore no table for these checks to inspect.
INVOICE_ID="$(curl -fsS "http://127.0.0.1:$PORT/invoices" | python3 -c '
import json,sys
rows = json.load(sys.stdin)
evaluated = [row for row in rows if row["state"] not in ("RECEIVED", "EVIDENCE_CHECKING")]
print((evaluated or rows)[0]["invoice"]["id"])')"

# Fail loudly if the fixture came out empty, rather than passing every check vacuously.
SETTLED="$(curl -fsS "http://127.0.0.1:$PORT/payments" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["payments"]))')"
if [ "$SETTLED" -lt 1 ]; then
  echo "the demo produced no payments, so the settlement log and its outcome words go unchecked" >&2
  exit 3
fi
echo "demo fixture: seeded, one worker pass, $SETTLED payment(s) in the settlement log"

# The definitions a term can be drawn from. Any term not in this list draws an empty popover.
CONCEPT_KEYS="$WORKDIR/concept-keys.txt"
curl -fsS "http://127.0.0.1:$PORT/explanations.json" \
  | python3 -c 'import json,sys; print("\n".join(sorted(json.load(sys.stdin)["concepts"])))' > "$CONCEPT_KEYS"

failures=0
printf '%-14s %-7s %-6s %s\n' view cards terms verdict
for view in "${VIEWS[@]}" "invoice/$INVOICE_ID"; do
  dom="$(chromium --headless --disable-gpu --no-sandbox --virtual-time-budget=6000 \
    --dump-dom "http://127.0.0.1:$PORT/console/#/$view" 2>/dev/null || true)"
  # `grep -o` with `wc -l` counts occurrences rather than lines, since the dumped DOM is one line.
  # `|| true` is load-bearing: with `pipefail` a zero-match grep would abort the very run this
  # script exists to report.
  cards="$(printf '%s' "$dom" | grep -o 'class="card"' | wc -l || true)"
  ledes="$(printf '%s' "$dom" | grep -o 'class="lede"' | wc -l || true)"
  terms="$(printf '%s' "$dom" | grep -o 'data-concept="[a-z_]*"' | sed 's/.*="//; s/"$//' | sort -u || true)"
  # A column heading may now be a term node, so the phone reflow label could silently become
  # "[object HTMLSpanElement]" and only a browser would ever show it.
  objects="$(printf '%s' "$dom" | grep -o 'data-label="\[object' | wc -l || true)"
  if [ -n "$terms" ]; then
    term_count="$(printf '%s\n' "$terms" | wc -l)"
    undefined="$(printf '%s\n' "$terms" | comm -23 - "$CONCEPT_KEYS" | tr '\n' ' ' || true)"
  else
    term_count=0
    undefined=""
  fi
  case "$view" in
    attention|queue|payments|worker|treasury|setup|invoice*) guided=1 ;;
    *) guided=0 ;;
  esac

  if printf '%s' "$dom" | grep -q 'Request failed'; then
    reason="$(printf '%s' "$dom" | sed -n 's/.*Request failed<\/strong><div>\([^<]*\).*/\1/p' | head -1 || true)"
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" "FAILED: ${reason:-threw}"
    failures=$((failures + 1))
  elif [ "$cards" -lt 1 ]; then
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" "FAILED: rendered nothing"
    failures=$((failures + 1))
  elif [ -n "$undefined" ]; then
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" "FAILED: a term has no definition: $undefined"
    failures=$((failures + 1))
  elif [ "$objects" -gt 0 ]; then
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" "FAILED: a column label rendered as a node"
    failures=$((failures + 1))
  elif [ "$guided" -eq 1 ] && { [ "$term_count" -lt 1 ] || [ "$ledes" -lt 1 ]; }; then
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" "FAILED: guided view rendered no explanations"
    failures=$((failures + 1))
  else
    printf '%-14s %-7s %-6s %s\n' "$view" "$cards" "$term_count" ok
  fi
done

if [ "$failures" -ne 0 ]; then
  echo "$failures view(s) did not render" >&2
  exit 1
fi
echo "every view rendered"
