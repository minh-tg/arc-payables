#!/usr/bin/env bash
# Shared isolation for credential-free browser smoke checks. Source from a smoke script.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORKDIR="$(mktemp -d -t arc-browser-smoke-XXXXXX)"
SERVER_PID=""
IDP_PID=""
SESSION="s$$"
mkdir -p "$WORKDIR/browser-home"
printf '{}\n' > "$WORKDIR/browser-config.json"
smoke_cleanup() {
  local status="$?"
  if [ "$status" -ne 0 ]; then timeout 10 "${BROWSER[@]}" snapshot --compact >&2 2>/dev/null || true; fi
  timeout 10 "${BROWSER[@]}" close >/dev/null 2>&1 || true
  for pid in "$SERVER_PID" "$IDP_PID"; do
    if [ -n "$pid" ]; then kill "$pid" 2>/dev/null || true; wait "$pid" 2>/dev/null || true; fi
  done
  rm -rf "$WORKDIR"
}
trap smoke_cleanup EXIT
command -v agent-browser >/dev/null
command -v chromium >/dev/null
BROWSER=(env -i PATH="$PATH" HOME="$WORKDIR/browser-home" LANG="${LANG:-C.UTF-8}"
  AGENT_BROWSER_DEFAULT_TIMEOUT=10000 AGENT_BROWSER_SOCKET_DIR="$WORKDIR/sockets" agent-browser --session "$SESSION" --namespace "$SESSION"
  --config "$WORKDIR/browser-config.json" --executable-path "$(command -v chromium)"
  --args "--no-sandbox,--disable-dev-shm-usage")
browser() { timeout 40 "${BROWSER[@]}" "$@"; }
check() { browser eval "(async()=>{$1;return null;})()"; }
ready() { browser wait --fn "document.querySelector('#view')?.getAttribute('aria-busy') === 'false'"; }
smoke_require_free_port() {
  python3 - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
if not 1024 <= port <= 65535:
    raise SystemExit("Smoke port must be between 1024 and 65535")
with socket.socket() as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        raise SystemExit(f"Smoke port {port} is occupied; choose another PORT, never reuse its server")
PY
}
# Even an existing project .env, exported live credentials or local browser config cannot affect
# these applications. uv --project selects dependencies without changing the disposable cwd.
MOCK_COMMAND=(env -i PATH="$PATH" HOME="$HOME" LANG="${LANG:-C.UTF-8}" ENVIRONMENT=local AUTH_MODE=demo
  PAYMENT_PROVIDER=mock ACCOUNTING_PROVIDER=mock SCREENING_PROVIDER=fixture DECISION_LAYER=heuristics
  SIGNER_BACKEND=env API_KEY= APPROVAL_TOKEN= WORKER_AUTOPAY=false CRITICAL_SUPPLIER_IDS='[]'
  MIN_RESERVE_USDC=2000 MAX_INVOICE_USDC=1000 DATABASE_PATH="$WORKDIR/demo.sqlite3"
  BACKUP_DIRECTORY="$WORKDIR/backups" uv run --project "$ROOT" --frozen)
mock_command() { "${MOCK_COMMAND[@]}" "$@"; }
# For a background server the recorded PID must be uv itself, not a parent shell function.
mock_server() { exec "${MOCK_COMMAND[@]}" "$@"; }
cd "$WORKDIR"
