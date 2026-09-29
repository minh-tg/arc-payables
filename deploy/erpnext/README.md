# Disposable ERPNext sandbox (Podman host instructions)

Run these commands in your **normal host terminal**, not the restricted agent shell. No
`sudo`, Podman migration, container pruning, or reboot is needed. This project has its own
Compose name, volumes, and network; it does not reuse your existing application's data.

This uses upstream ERPNext images, not a fork. ERPNext is pinned to `v15.121.5` (tag verified
against Docker Hub). The configuration follows the upstream
[frappe_docker example](https://github.com/frappe/frappe_docker/blob/main/pwd.yml), with a
manual site-creation step and a localhost-only port. The operator created the site, dashboard
and USD company, and the bootstrap has since been run against that live instance:

**Verified on the live sandbox** (ERPNext v15.121.5, site `tameion.localhost`, company
`Arc Demo Inc`/`ADI`, time zone `America/Moncton`):

| Record | Result |
| --- | --- |
| `PUR-ORD-2026-00011` | submitted, *Completed*, 250.00 |
| `MAT-PRE-2026-00001` | submitted, *Completed*, 250.00 |
| `ACC-PINV-2026-00007` | **submitted**, *Unpaid*, 250.00, columns linked to the PO and PR lines |
| GL entries | Dr *Stock Received But Not Billed* 250.00 / Cr *Creditors* 250.00 |
| Supplier wallet | stored, `custom_usdc_wallet_verified = 0` (still unverified) |
| Payment Entry | none, because provisioning never pays |
| Repeat run | no creates or submissions; fully idempotent |

Two live-integration defects were found and fixed as a result of this run:

* **Site time zone.** Posting dates must be compared against the *site's* time zone. The host
  clock here is UTC while the site's zone is `America/Moncton`, so "today" differed and ERPNext
  rejected the receipt with `417` (a future stock posting). The tool now reads the site time
  zone, defaults `--posting-date` to the site's local date, and refuses a future date.
* **Submission contract.** Frappe v15's `frappe.client.submit` reinstantiates whatever it
  receives, so sending only `doctype`/`name` submits nothing. Both the bootstrap and the
  runtime payment-entry writeback now send the whole document; the runtime path additionally
  refuses to report a completed writeback unless ERPNext confirms it.

A third gap was found in the deterministic policy: ERPNext reports a *submitted* Purchase Order
and Receipt as `Completed`, `To Bill` or `To Receive and Bill`, never the literal `Submitted`,
so the policy's accepted status vocabulary now matches the real values (`Completed`, `Closed`,
`To Bill`, `To Receive`, `To Receive and Bill`, `Submitted`), while `Draft`, `On Hold`,
`Cancelled`, `Return Issued` and `Returned` still block payment.

> Local disposable development only. MariaDB uses the intentionally public demo root
> password `admin` unless `MYSQL_ROOT_PASSWORD` is overridden. Database/Redis ports are not
> published. Only ERPNext HTTP is published, on **127.0.0.1:8080**. Do not expose this stack
> through a tunnel, public proxy, or LAN binding. Do not enter real supplier data or payment
> credentials. The ERPNext UI here is the external ERP application, not a Arc Payables frontend.

## 1. Pull and start the services

```bash
cd deploy/erpnext   # from the repository root
podman-compose --version
podman-compose pull
podman-compose up -d
podman-compose ps
```

Stop if a command fails. If `podman-compose` is unavailable on NixOS, run
`nix shell nixpkgs#podman-compose` and retry in that shell.

The image download can take several minutes and consume several GB after extraction.
Port 8080 must be free. This starts the database, Redis, configurator, backend, workers,
scheduler, websocket server and ERPNext HTTP frontend. It does **not** create a site.

`configurator` exiting with **code 0 is expected**: it writes shared configuration once.
The backend healthcheck may fail until the site below exists. Database readiness is a
backend startup dependency. No `create-site` service exists in this Compose file.

## 2. Create the site once

```bash
podman-compose exec backend bench new-site tameion.localhost \
  --db-host db \
  --db-root-username root \
  --mariadb-user-host-login-scope='%' \
  --install-app erpnext \
  --set-default
```

Answer the prompts **locally**:

- Database root password: `admin`, unless you explicitly changed `MYSQL_ROOT_PASSWORD`.
- New ERPNext Administrator password: choose a throwaway password and keep it private.

The `%` login scope lets Frappe's site database user connect from the other containers;
MariaDB is not published to the host. Passwords are prompted rather than placed in command
arguments. No API tokens, Circle entity secret, wallet key or policy-signing key are needed.

Wait for the command to complete. On subsequent starts, skip this step. If creation reports
that the site already exists, check `list-apps` below; **do not use `--force`**. An interrupted
installation needs diagnosis, not an automatic overwrite of its database.

## 3. Complete the setup wizard

Open **http://127.0.0.1:8080** and log in as `Administrator` using the password you chose.
Use the wizard to create the company and chart of accounts:

| Field | Demo value |
| --- | --- |
| Company name | `Arc Demo Inc` |
| Company abbreviation | `ADI` |
| Country | Your preferred country |
| Company/base currency | **USD** |
| Chart of accounts | Standard template for the selected country |

Skip optional demo transactions if offered. USDC will later be represented as the settlement
asset; do not change the company's base currency to USDC.

Administrator is used only for this local ERPNext setup. A separate, scoped integration user
will be configured later. Keep API credentials out of chat and screenshots.

## 4. Verify startup and the USD company

```bash
podman-compose exec backend bench --site tameion.localhost list-apps
curl --fail --show-error http://127.0.0.1:8080/api/method/ping
```

`list-apps` should include both `frappe` and `erpnext`. The HTTP response should be
`{"message":"pong"}`. These prove site/application startup, **not** accounting correctness,
company setup, or a complete payment integration. Confirm the wizard also completed.

Report the app versions, the ping result, and whether the USD company was created. Do not
send passwords, API secrets, site configuration files, or private keys.

If setup initially created a CAD company, leave it intact and add a separate USD company.
Never convert an existing CAD company or assume CAD/USDC parity. With multiple companies,
all provisioning commands must explicitly select the USD company.

Read the selected company's non-secret defaults from your host terminal:

```bash
podman-compose exec backend bench --site tameion.localhost execute frappe.get_all \
  --kwargs '{"doctype":"Company","filters":{"name":"Arc Demo Inc"},"fields":["name","abbr","default_currency","default_payable_account","default_expense_account","cost_center"]}'
```

Expect exactly one USD company, with nonempty default payable/expense accounts and a cost
center. The bootstrap also requires an active `Stores - <abbreviation>` warehouse and the
standard `Nos` UOM. Missing or ambiguous settings are rejected, not guessed. This command
needs no API key and does not print passwords or site configuration. Stop and resolve any
missing defaults before provisioning.

## 5. Provisioning preview and safeguards

The local bootstrap suite is passing, including lost-response, draft-resume, currency,
wallet-conflict, read-only preview and site-time-zone tests. Its outgoing document/child
fields were checked against upstream Frappe/ERPNext v15 schemas, and the run described at the
top of this file then exercised the real ERPNext validators end to end. A preview still does
not execute those validators, so review it rather than trusting it blindly.

Provisioning is an **operator-only, one-time setup task**. A dedicated disposable setup user
needs permissions for Currency, Custom Field, Account, Mode of Payment, Supplier, Item, and
create/read/submit on Purchase Order, Purchase Receipt and Purchase Invoice, plus read access
to the selected Company, groups, warehouse, UOM and cost center. Creating Custom Fields needs
administrative setup permissions. Do not give those privileges or credentials to the agent
or to the later runtime accounting user. Review permissions locally before generating any
credentials, and revoke the setup user's access when provisioning is finished.

Two access mistakes cost real debugging time on the live sandbox, so check both up front:

* **Leave Role Profile empty.** A user saved with a Role Profile has its role table replaced
  by that profile's roles on every save, so roles you add silently disappear. The symptom is
  misleading: `frappe.auth.get_logged_user` returns `200` while every other call returns
  `403`. The user here was created with an empty profile that happened to be called
  `System Manager`, which is what emptied its roles. After saving, confirm
  `frappe.get_roles(...)` lists your roles and that `user_type` became `System User`.
* **`Supplier` create needs `Purchase Master Manager`.** `Purchase Manager` grants read/write
  only on Supplier, so the demo-supplier insert fails without it.

The CLI reads only `FRAPPE_URL`, `FRAPPE_API_KEY` and `FRAPPE_API_SECRET` from its environment;
it does not load `.env` or any payment/signing configuration. Set credentials privately in
your own terminal, never in command arguments, chat, screenshots, source control, or logs.
HTTPS is required except for a localhost HTTP sandbox.

Once the operator-only credentials and company prerequisites are in place, run from the
repository root. Set `DEMO_SUPPLIER_WALLET` to your test-only recipient and
`DEMO_POSTING_DATE` to the intended `YYYY-MM-DD` posting date first; keep both unchanged on
retries. No real supplier, Circle entity secret, or signing key is needed.

```bash
export FRAPPE_URL=http://127.0.0.1:8080
uv run arc-payables-erpnext-bootstrap \
  --company 'Arc Demo Inc' \
  --wallet "$DEMO_SUPPLIER_WALLET" \
  --posting-date "$DEMO_POSTING_DATE" \
  --invoice-reference ARC-PAYABLES-DEMO-001 \
  --dry-run
```

Preview is the default even without `--dry-run`. It issues only GET requests, prints a
plan, and marks synthetic IDs as `PLANNED-*`; these are not real Purchase Invoice IDs.
After reviewing the preview, using the same arguments with **`--apply` instead of
`--dry-run`** explicitly creates/submits the sandbox records. Do not run concurrent
provisioning sessions. No payment or Payment Entry is created by this command.

The bootstrap:

- Refuses non-USD or missing company currency before any write. USD/USDC 1:1 is an explicit
  **demo assumption**, not production FX/accounting guidance.
- Creates the USDC currency metadata, USDC bank/settlement account, USD network-fee expense
  account, and company-specific demo Mode of Payment.
- Creates a company-scoped demo Supplier and demo stock item. A new supplier wallet remains
  **unverified**; it must be verified separately by an authorized human in ERPNext. An
  existing wallet is never replaced or automatically re-verified.
- Uses unique seed fields on PO/PR/PI, keyed by company and invoice reference. It checks
  authoritative child-row links, quantities, rates, taxes, totals and submission states
  before reusing or submitting a document. Conflicting/cancelled/paid records stop the run.
- Sends the full validated document to `frappe.client.submit`, including its concurrency
  timestamp and child rows, rather than just a document name.
- Never retries a write automatically. After a lost response, rerun with the **same company,
  reference, wallet, date and amounts** so it can find and validate the existing records.
  There is no global transaction across REST requests: partial drafts/master records may
  remain on failure. Do not delete them, change the reference, or retry blindly to hide an
  error; review the stopped operation first.
- Prints candidate account mappings only; it does not activate Frappe writeback. Verify
  actual GL behavior, six-decimal settlement/fee precision, and separate absorbed fee
  expense before enabling writeback. The supplier's authorized amount must stay exact.

### The state this produced on the live sandbox

Running with `--posting-date 2026-09-28` (the site's local date) created: `USDC` currency
metadata (`fraction_units = 1000000`), `USDC Wallet - ADI` (asset, USDC), `Network Fees - ADI`
(expense, USD), `Arc Testnet Demo - ADI` mode of payment, five custom fields, the demo supplier,
the demo item, and the submitted `PUR-ORD-2026-00011` / `MAT-PRE-2026-00001` /
`ACC-PINV-2026-00007`. Re-running with identical arguments created and submitted nothing.

One ERPNext behaviour to expect rather than "fix": this company has **perpetual inventory
enabled**, so ERPNext books each invoice line to the interim `Stock Received But Not Billed - ADI`
account instead of the company expense account, overriding whatever is sent. The bootstrap
therefore neither sends nor asserts that field. The network-fee account used by the
payment-entry deduction is a different account and is unaffected.

Next step is **not** live Circle/Arc money. It is a separate, narrowly scoped runtime user plus
verification of the connector's own Payment Entry writeback against this sandbox, using the mock
payment provider. Only then does the Arc Testnet credential work make sense.

## Troubleshooting

### First site creation fails with `using password: NO`, then `Site ... already exists`

`using password: NO` means MariaDB received no password, not that it rejected a nonempty
password. Hidden password input does not echo characters. Frappe creates its site directory
and configuration before attempting the database connection, so an early authentication
failure can leave a directory that blocks the next attempt.

The recovery below is **only for this fresh sandbox**, when the first attempt failed at
`SHOW DATABASES` before any database or app installation. It is not a reset procedure for
an existing ERPNext site. Do not use `--force`, `drop-site`, or `down -v`.

Preserve the partial site inside the same volume, outside Frappe's top-level site discovery:

```bash
podman-compose exec -T backend bash -ec '
  cd /home/frappe/frappe-bench
  test -d sites/tameion.localhost
  mkdir -p sites/.failed-sites
  backup=$(mktemp -d sites/.failed-sites/tameion.localhost.XXXXXX)
  mv -- sites/tameion.localhost "$backup/site"
  printf "Partial site preserved at %s/site\n" "$backup"
'
```

Retry with the **intentionally public demo database password** explicitly supplied:

```bash
podman-compose exec backend bench new-site tameion.localhost \
  --db-host db \
  --db-root-username root \
  --db-root-password admin \
  --mariadb-user-host-login-scope='%' \
  --install-app erpnext \
  --set-default
```

Use that flag only for the public demo password. If you changed the database password,
omit `--db-root-password admin` and enter your password at the hidden prompt locally. Never
put a real password into a command argument or share it here. The ERPNext Administrator
password is still prompted; choose and confirm it locally. If the retry fails, stop and
share only the redacted error, rather than repeatedly archiving or forcing creation.

### Other startup failures

If startup fails, collect bounded logs:

```bash
podman-compose ps
podman-compose logs --tail=80 configurator db backend frontend
```

Inspect and redact passwords/tokens before sharing logs. If Podman says the database is not
yet healthy, check its logs, wait for initialization, then retry `podman-compose up -d`.
A 404 before site creation is expected; a persistent 502 after site creation needs backend
and frontend logs. Do not use privileged containers or change host UID/GID mappings to fix
this stack.

## Stop or delete only this sandbox

Run these only from this directory (or specify this Compose file explicitly):

```bash
podman-compose stop        # stop services; preserve data
podman-compose down        # remove this stack's containers/network; preserve named volumes
```

**Destructive reset**, only if you intentionally want to discard the sandbox's entire ERP
site, uploaded files and database:

```bash
podman-compose down -v
```

Never use `podman system reset` or global pruning for this setup; unrelated containers are
not part of this project.
