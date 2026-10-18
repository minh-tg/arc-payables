#!/usr/bin/env bash
# One non-interactive setup pass over the disposable ERPNext sandbox in deploy/erpnext.
#
#   bash script/erpnext-sandbox-setup.sh            # set the sandbox up, skipping every step already done
#   bash script/erpnext-sandbox-setup.sh --dry-run  # print the numbered plan and the container tool, change nothing
#   bash script/erpnext-sandbox-setup.sh --help
#
# The pass starts the Compose stack, creates the site tameion.localhost, completes the setup wizard,
# creates the integration user ap-agent@arc-demo.test with its API keys, and runs the ERPNext
# bootstrap for the demo purchase invoice. A step whose result already exists is reported and
# skipped. Nothing is forced, no site is dropped, no volume is removed.
#
# Two private files are written (mode 600, inside the git-ignored .secret/):
#   .secret/erpnext-admin-password.txt  the ERPNext Administrator password, generated once
#   .secret/erpnext-sandbox.env         FRAPPE_URL, the integration API key and secret, and the
#                                       non-secret FRAPPE_* mapping lines the bootstrap prints
# No password, key or secret is printed by this script, and none of them appears in the tree.
#
# Sandbox only: FRAPPE_URL must be http://127.0.0.1 or http://localhost. The manual steps and
# troubleshooting remain in deploy/erpnext/README.md.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SECRET_DIR="$ROOT/.secret"
ADMIN_PASSWORD_FILE="$SECRET_DIR/erpnext-admin-password.txt"
ENV_FILE="$SECRET_DIR/erpnext-sandbox.env"
SITE="tameion.localhost"
COMPANY="Arc Demo Inc"
COMPANY_ABBR="ADI"
AGENT_EMAIL="ap-agent@arc-demo.test"
DEMO_WALLET="0x1111111111111111111111111111111111111111"
INVOICE_REFERENCE="ARC-PAYABLES-DEMO-001"
SITE_URL="${FRAPPE_URL:-http://127.0.0.1:8080}"
DRY_RUN=0
TMPDIR_RUN=""
COMPOSE=()
TOOL_LABEL=""

usage() {
  local code="$1"
  if [ "$code" -eq 0 ]; then
    cat <<'USAGE'
Usage: bash script/erpnext-sandbox-setup.sh [--dry-run] [--help]

  --dry-run  print the numbered plan and the container tool, then exit
  --help     print this text

With no argument the script sets the sandbox up in one pass. It needs python3 and uv on PATH,
and either podman-compose or docker compose.
USAGE
  else
    cat >&2 <<'USAGE'
Usage: bash script/erpnext-sandbox-setup.sh [--dry-run] [--help]
USAGE
  fi
  exit "$code"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --help|-h) usage 0 ;;
    *) echo "Unknown argument: $1" >&2; usage 2 ;;
  esac
done

# Loopback only. This refuses before any container command runs, so a remote URL can never
# start a stack or send a request.
if [[ ! "$SITE_URL" =~ ^http://(127\.0\.0\.1|localhost)(:[0-9]+)?$ ]]; then
  echo "FRAPPE_URL must be http://127.0.0.1 or http://localhost. This sandbox never talks to a remote ERPNext." >&2
  exit 2
fi

step() { printf '%s. %s\n' "$1" "$2"; }

# Step 1. Pick the container tool. `podman ps` fails with a user-namespace error on this host, so
# the fallback sends the same commands through the Podman API socket instead.
choose_container_tool() {
  if [ "$DRY_RUN" -eq 1 ]; then
    # The dry run probes PATH only: no container command is started.
    if command -v podman-compose >/dev/null 2>&1; then
      TOOL_LABEL="podman-compose (the Podman API socket wrapper if podman ps fails)"
    elif command -v docker >/dev/null 2>&1; then
      TOOL_LABEL="docker compose"
    else
      TOOL_LABEL="none found; install podman-compose or docker compose"
    fi
    return 0
  fi
  if command -v podman-compose >/dev/null 2>&1; then
    if command -v podman >/dev/null 2>&1 && podman ps >/dev/null 2>&1; then
      COMPOSE=(podman-compose)
      TOOL_LABEL="podman-compose"
    elif command -v podman >/dev/null 2>&1; then
      local wrapper="$TMPDIR_RUN/podman-api-socket"
      cat > "$wrapper" <<'WRAPPER'
#!/usr/bin/env bash
# podman through the existing API socket, because the CLI cannot join its user namespace here
exec podman --remote --url "unix:///run/user/$(id -u)/podman/podman.sock" "$@"
WRAPPER
      chmod 700 "$wrapper"
      COMPOSE=(podman-compose --podman-path "$wrapper")
      TOOL_LABEL="podman-compose with the Podman API socket wrapper"
    else
      echo "podman-compose is on PATH but podman is not. Install podman or use docker compose." >&2
      exit 2
    fi
  elif command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
    TOOL_LABEL="docker compose"
  else
    echo "Neither podman-compose nor docker compose is available. On NixOS run 'nix shell nixpkgs#podman-compose' and retry." >&2
    exit 2
  fi
}

# The compose file carries its own project name, so every command runs from deploy/erpnext with
# no -f and no -p, exactly as the README documents.
compose() { "${COMPOSE[@]}" "$@"; }

wait_for_backend() {
  local attempt
  printf 'Waiting for the backend container to see the bench directory'
  for attempt in $(seq 1 60); do
    if compose exec -T backend bash -c 'test -f /home/frappe/frappe-bench/sites/apps.txt && test -d /home/frappe/frappe-bench/sites' >/dev/null 2>&1; then
      printf ' ready.\n'
      return 0
    fi
    printf '.'
    sleep 5
  done
  printf '\n'
  echo "The backend did not become ready within five minutes. Inspect 'podman-compose ps' and 'podman-compose logs --tail=80 configurator db backend'." >&2
  exit 1
}

site_exists() {
  compose exec -T backend bash -c "test -d /home/frappe/frappe-bench/sites/$SITE" >/dev/null 2>&1
}

# frappe.client.get_value with a filter, printed by bench execute. Read-only.
bench_get_value() {
  compose exec -T backend bench --site "$SITE" execute frappe.client.get_value --kwargs "$1" 2>/dev/null || true
}

company_exists() {
  local out
  out="$(bench_get_value "{\"doctype\":\"Company\",\"filters\":{\"name\":\"$COMPANY\"},\"fieldname\":[\"name\"]}")"
  [[ "$out" == *"$COMPANY"* ]]
}

integration_user_exists() {
  local out
  out="$(bench_get_value "{\"doctype\":\"User\",\"filters\":\"$AGENT_EMAIL\",\"fieldname\":[\"name\"]}")"
  [[ "$out" == *"$AGENT_EMAIL"* ]]
}

# Reads one value out of the existing credentials file, for the key-reuse check in step 5.
env_value() {
  local name="$1" line value=""
  [ -r "$ENV_FILE" ] || return 0
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ ^${name}=(.*)$ ]]; then
      value="${BASH_REMATCH[1]}"
      if [[ "$value" =~ ^\'(.*)\'$ ]]; then value="${BASH_REMATCH[1]}"; fi
    fi
  done < "$ENV_FILE"
  printf '%s' "$value"
}

# Step 3 helper: one generated Administrator password, kept across reruns so a site created by an
# earlier run still has a known password. The value never leaves the file.
ensure_admin_password() {
  umask 077
  mkdir -p "$SECRET_DIR"
  if [ ! -s "$ADMIN_PASSWORD_FILE" ]; then
    python3 -c 'import secrets; print(secrets.token_urlsafe(24))' > "$ADMIN_PASSWORD_FILE"
  fi
  chmod 600 "$ADMIN_PASSWORD_FILE"
}

# Step 5 helper: write URL, key and secret as the first three lines, mode 600. Values arrive
# through the environment of the child process, so they stay out of this script's output.
write_env_header() {
  local key="$1" secret="$2"
  umask 077
  SETUP_URL="$SITE_URL" SETUP_KEY="$key" SETUP_SECRET="$secret" python3 - "$ENV_FILE" <<'PY'
import os, pathlib, sys

target = pathlib.Path(sys.argv[1])
target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(f"FRAPPE_URL={os.environ['SETUP_URL']}\n")
    handle.write(f"FRAPPE_API_KEY={os.environ['SETUP_KEY']}\n")
    handle.write(f"FRAPPE_API_SECRET={os.environ['SETUP_SECRET']}\n")
os.chmod(target, 0o600)
PY
}

# Step 5 helper: pull api_key and api_secret out of the JSON bench execute printed.
keys_to_env_file() {
  local keys_file="$1"
  SETUP_KEYS_FILE="$keys_file" SETUP_URL="$SITE_URL" python3 - "$ENV_FILE" <<'PY'
import json, os, pathlib, sys

raw = pathlib.Path(os.environ["SETUP_KEYS_FILE"]).read_text(errors="replace")
data = None
for line in reversed(raw.splitlines()):
    line = line.strip()
    if line.startswith("{"):
        data = json.loads(line)
        break
if not data or not data.get("api_key") or not data.get("api_secret"):
    raise SystemExit("Frappe did not return an API key and secret for the integration user.")

target = pathlib.Path(sys.argv[1])
fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as handle:
    handle.write(f"FRAPPE_URL={os.environ['SETUP_URL']}\n")
    handle.write(f"FRAPPE_API_KEY={data['api_key']}\n")
    handle.write(f"FRAPPE_API_SECRET={data['api_secret']}\n")
os.chmod(target, 0o600)
PY
}

print_plan() {
  step 1 "Container tool: $TOOL_LABEL"
  step 2 "Pull and start deploy/erpnext/docker-compose.yml (project tameion-erpnext-sandbox)"
  step 3 "Create site $SITE if it is absent, with a generated Administrator password kept in .secret/erpnext-admin-password.txt"
  step 4 "Complete the setup wizard non-interactively if company $COMPANY ($COMPANY_ABBR) is absent (USD, America/Moncton)"
  step 5 "Create integration user $AGENT_EMAIL with the demo roles if absent and write .secret/erpnext-sandbox.env with its API key and secret"
  step 6 "Run arc-payables-erpnext-bootstrap for $COMPANY with the demo wallet and reference $INVOICE_REFERENCE --apply"
  step 7 "Print the sandbox URL, the two private file paths and the next command"
}

if [ "$DRY_RUN" -eq 1 ]; then
  choose_container_tool
  print_plan
  echo "Dry run: no container, site, user or bootstrap command was started."
  exit 0
fi

command -v python3 >/dev/null || { echo "python3 is required to generate the Administrator password." >&2; exit 2; }
command -v uv >/dev/null || { echo "uv is required for the bootstrap step." >&2; exit 2; }
# Scratch space for the generated Podman wrapper and the bootstrap output; removed on exit.
TMPDIR_RUN="$(mktemp -d -t erpnext-sandbox-setup-XXXXXX)"
trap 'rm -rf "$TMPDIR_RUN"' EXIT
choose_container_tool
step 1 "Container tool: $TOOL_LABEL"

cd "$ROOT/deploy/erpnext"
step 2 "Pulling and starting the stack"
compose pull
compose up -d
compose ps

step 3 "Site $SITE"
wait_for_backend
if site_exists; then
  echo "   Site $SITE already exists; left untouched."
else
  ensure_admin_password
  compose exec -T backend bench new-site "$SITE" \
    --db-host db --db-root-username root --db-root-password admin \
    --admin-password "$(cat "$ADMIN_PASSWORD_FILE")" \
    --mariadb-user-host-login-scope='%' --install-app erpnext --set-default
  echo "   Site $SITE created; the Administrator password is in $ADMIN_PASSWORD_FILE."
fi

step 4 "Setup wizard (USD, $COMPANY, $COMPANY_ABBR)"
if company_exists; then
  echo "   Company $COMPANY already exists; wizard skipped."
else
  # The same argument list the manual wizard produces, so the demo records keep their identity.
  WIZARD_ARGS='[{"language":"English","country":"United States","timezone":"America/Moncton","currency":"USD","full_name":"Arc Demo Operator","company_name":"'"$COMPANY"'","company_abbr":"'"$COMPANY_ABBR"'","chart_of_accounts":"Standard","fy_start_date":"2026-01-01","fy_end_date":"2026-12-31","domains":["Distribution"]}]'
  compose exec -T backend bench --site "$SITE" execute \
    frappe.desk.page.setup_wizard.setup_wizard.setup_complete --args "$WIZARD_ARGS"
  echo "   Wizard completed."
fi

step 5 "Integration user $AGENT_EMAIL"
if integration_user_exists; then
  echo "   User $AGENT_EMAIL already exists."
else
  # Role Profile stays empty: a saved profile rewrites the role table and drops these roles.
  USER_DOC='{"doc":{"doctype":"User","email":"'"$AGENT_EMAIL"'","first_name":"AP Agent","send_welcome_email":0,"user_type":"System User","enabled":1,"roles":[{"role":"System Manager"},{"role":"Accounts Manager"},{"role":"Accounts User"},{"role":"Purchase Manager"},{"role":"Purchase Master Manager"},{"role":"Purchase User"},{"role":"Stock Manager"},{"role":"Stock User"},{"role":"Item Manager"}]}}'
  compose exec -T backend bench --site "$SITE" execute frappe.client.insert --kwargs "$USER_DOC" >/dev/null
  echo "   User $AGENT_EMAIL created."
fi
# Reuse the key pair already recorded in the private file, otherwise generate a new one. Either way
# the pair reaches the file and nothing else.
reused_key="$(env_value FRAPPE_API_KEY)"
reused_secret="$(env_value FRAPPE_API_SECRET)"
if [ -n "$reused_key" ] && [ -n "$reused_secret" ]; then
  write_env_header "$reused_key" "$reused_secret"
  echo "   Kept the existing API key pair for $AGENT_EMAIL."
else
  KEYS_JSON="$TMPDIR_RUN/keys.json"
  compose exec -T backend bench --site "$SITE" execute \
    frappe.core.doctype.user.user.generate_keys --kwargs '{"user":"'"$AGENT_EMAIL"'"}' > "$KEYS_JSON"
  keys_to_env_file "$KEYS_JSON"
  rm -f "$KEYS_JSON"
  echo "   Generated an API key pair for $AGENT_EMAIL."
fi
echo "   Credentials: $ENV_FILE"

step 6 "ERPNext bootstrap (--apply)"
POSTING_DATE="$(TZ=America/Moncton date +%F)"
bootstrap_run() {
  set -a
  FRAPPE_URL="$SITE_URL"
  FRAPPE_API_KEY="$(env_value FRAPPE_API_KEY)"
  FRAPPE_API_SECRET="$(env_value FRAPPE_API_SECRET)"
  set +a
  uv run --project "$ROOT" --frozen arc-payables-erpnext-bootstrap \
    --company "$COMPANY" --wallet "$DEMO_WALLET" --posting-date "$POSTING_DATE" \
    --invoice-reference "$INVOICE_REFERENCE" --apply
}
cd "$ROOT"
bootstrap_run | tee "$TMPDIR_RUN/bootstrap.out"
# The bootstrap prints shlex-quoted FRAPPE_* mapping lines after its "Candidate demo mappings"
# header. They carry accounts, currencies, cost centre and mode of payment, never a credential.
awk '/^FRAPPE_[A-Z0-9_]+=/ && !/^FRAPPE_(URL|API_KEY|API_SECRET)=/' "$TMPDIR_RUN/bootstrap.out" >> "$ENV_FILE"
chmod 600 "$ENV_FILE"
if ! grep -q '^FRAPPE_COMPANY=' "$ENV_FILE"; then
  echo "The bootstrap printed no account mappings; $ENV_FILE holds only the URL and the key pair. Check the output above." >&2
  exit 1
fi
echo "   Account mappings appended to $ENV_FILE."

step 7 "Summary"
printf 'ERPNext:                 %s\n' "$SITE_URL"
printf 'Administrator password:  %s (mode 600, not printed)\n' "$ADMIN_PASSWORD_FILE"
printf 'Integration credentials: %s (mode 600, not printed)\n' "$ENV_FILE"
printf 'Next:                    bash script/erpnext-demo.sh\n'
