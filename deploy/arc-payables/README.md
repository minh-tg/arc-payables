# Supervised Arc Payables service

This Compose stack runs one API and one worker on one host. Both use the same SQLite database volume. The API starts first. The worker waits until `/ready` succeeds. Both services use the `unless-stopped` restart policy. The container engine must be active for that policy to take effect.

This is a single-host deployment. SQLite is not shared across hosts. Do not scale the worker service to multiple replicas. Payment writes are idempotent, but the worker loop is not a distributed queue.

## Configure and start

Run from the repository root. Podman Compose is used here because the project already uses it for the ERPNext sandbox.

```sh
cp .env.example .secret
chmod 600 .secret
```

Edit `.secret` for the target environment. It is git-ignored and excluded from the image build context. Do not put credentials in the Compose file or the Prometheus configuration. Keep `WORKER_AUTOPAY=false` unless unattended payment is an explicit operating decision. A command-line `--autopay` enables it for one worker process. `--no-autopay` disables it even when the environment setting is true.

Validate and start the stack:

```sh
podman-compose -f deploy/arc-payables/docker-compose.yml config --quiet
podman-compose -f deploy/arc-payables/docker-compose.yml up -d --build
podman-compose -f deploy/arc-payables/docker-compose.yml ps
```

The API binds to `127.0.0.1:8000`. Keep it behind a local reverse proxy or an authenticated private network if remote access is required. The compose stack does not publish the database or worker ports.

### Start after reboot

For rootless Podman, configure the `podman-compose` systemd user integration on the target host. The installed `podman-compose` version provides these actions:

```sh
sudo podman-compose systemd -a create-unit
podman-compose -p arc-payables -f deploy/arc-payables/docker-compose.yml systemd -a register
systemctl --user enable --now podman-compose@arc-payables.service
loginctl enable-linger "$USER"
```

Confirm the generated unit starts the stack after a reboot before relying on unattended operation. For Docker, enable the Docker service at boot; the Compose restart policy then restores the containers.

## Health and operation

The API healthcheck requests `/ready`. This checks database access and required provider configuration. It does not prove that the worker is running.

The worker healthcheck reads the latest `worker_runs.finished_at` value from the shared database. It marks the worker unhealthy if no pass was recorded in the last 15 minutes, or five worker intervals, whichever is longer. An unhealthy healthcheck is visible in `podman-compose ps`; Compose does not restart a still-running container just because its healthcheck failed. The restart policy handles process exit. Use `/worker/status` to inspect pass outcomes, alerts, and consecutive failures.

Open `http://127.0.0.1:8000/console/`, enter the API key, then select **Worker**. The view shows the last pass, its steps, alerts, and any alert delivery errors. The `API_KEY` protects `/worker/status` and `/metrics`.

Useful commands:

```sh
podman-compose -f deploy/arc-payables/docker-compose.yml logs --tail=100 api worker
podman-compose -f deploy/arc-payables/docker-compose.yml restart api worker
podman-compose -f deploy/arc-payables/docker-compose.yml down
```

`down` preserves the `arc-payables-data` volume. Do not use `down -v` unless deleting all invoice, payment, and audit history is intentional. Use SQLite's backup API to back up the live database. Do not copy only the `.sqlite3` file while the service is running because WAL transactions may still be in sidecar files.

## Metrics scrape

`prometheus.yml` is a host-local scrape example. Mount a file containing only the API key at `/run/secrets/arc-payables-api-key` in the Prometheus process. Restrict that file to the Prometheus user. Do not put the key in `prometheus.yml`. If Prometheus runs in a container, configure a private network route to the API; the API's host port is bound to loopback.

The scrape includes worker failure count, last pass time, unresolved settlements, audit-chain status, reserve headroom, and guard pause state. The configuration does not start Prometheus or send alerts to a vendor.

## Alert response

Alerts are stored on the pass and shown in the Worker view. A webhook is optional. Delivery errors are recorded separately and do not stop the worker.

| Alert | Operator action |
| --- | --- |
| `worker_failing` | Inspect the latest pass steps and worker logs. Correct the underlying provider or configuration fault. |
| `audit_chain_broken` | Stop payment operations. Preserve the database and investigate the audit history before resuming. |
| `reserve_breached` | Check the treasury balance and reserve configuration. Do not bypass the policy floor. |
| `settlements_unconfirmed` | Reconcile the transaction against Arc. Never resend an uncertain payment. |
| `payments_not_in_ledger` | Inspect ERPNext references and retry the idempotent writeback. Do not resubmit the chain payment. |

## Architecture and growth limits

`runtime.py` is the composition root shared by the API and worker. The worker builds the same workflow without importing or initializing the FastAPI app. `ports.py` defines the accounting, payment, signer, and evidence-store boundaries. The worker and API coordinate through the durable SQLite database, not process memory.

SQLite WAL mode allows API reads while a worker transaction commits. SQLite still allows only one writer at a time and this database must remain on one host. This deployment is tested with one API process and one worker process. Do not scale either service until cross-process adapter behavior and worker claiming have dedicated tests. A multi-host deployment requires a server database, a complete store implementation behind the evidence-store boundary, and tested transaction semantics for payment authorization and worker claims. It is not a Compose scaling switch.

The current application service and HTTP route module remain deliberately in-process. Split those modules when a new transport or independent deployable service needs the boundary. Do not split them only to make more containers.
