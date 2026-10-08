# Operations: backups, restore drills, alerting and ownership

This is the operational half of production readiness. It covers the controls that only matter when
something has already gone wrong: recovering the database, knowing how much data a recovery loses,
and having a person who is actually told.

Two facts shape everything here:

* The deployment is **one API and one worker on one host, sharing one SQLite database**. There is no
  distributed queue and no replica. That makes recovery simpler and the backup more important: there
  is exactly one copy of the evidence, the payments and the audit chain.
* **The audit chain is inside the database.** A backup that does not restore therefore loses the
  ability to prove what was authorized, not just the data.

Nothing here enables mainnet or changes a contract limit. The gates that remain external are listed
in [docs/production-settlement.md](production-settlement.md).

## Backups

```sh
# Consistent copy plus manifest, verifying the copy before it is published
uv run arc-payables-backup --destination /srv/backups/arc-payables --keep 48

# Ship it off-host: the hook receives the backup path and its failure fails the run
uv run arc-payables-backup --destination /srv/backups/arc-payables --keep 48 \
  --hook "rclone copy --immutable"
```

The copy is taken with SQLite's **online backup API**, so a running writer cannot tear it, and the
artefact is switched to a rollback journal so it is a **single self-contained file**. That detail is
not cosmetic: a copy left in WAL mode can be silently reinterpreted by a stray `-wal` file beside it,
while its checksum covers only the main file. The tool refuses a copy that is not self-contained.

Each run writes `*.sqlite3` plus `*.manifest.json` containing:

| Field | Why it is recorded |
| --- | --- |
| `sha256` | Detect a backup that changed after it was written. The restore drill refuses a mismatch. |
| `database.integrity_check` | SQLite's own verdict on the copy, taken before publication. |
| `database.audit` | The hash chain verifies and every recorded signature matches the key that entry names. |
| `database.counts` | Row counts per table; the drill compares the restored copy against these. |
| `database.recovery_point` | The newest timestamps the copy contains. **This is the RPO input.** |
| `duration_seconds`, `hook` | How long the backup took, and whether it actually left the host. |

`--keep N` prunes older backups with their manifests. A file with no manifest is never deleted: it
was not written by this tool, and removing it would hide the real problem.

**Encryption and off-host storage are the hook's job, not this tool's.** Name the tool you use
(`restic`, `rclone`, `age` with a recipient key) rather than letting backups accumulate on the host
that is about to fail. A failed hook fails the run, because a backup that stayed on the host is not
an off-host backup.

## RPO and RTO

Neither is a number this repository can set for you. Both are decisions, and the tools measure the
inputs so the decision can be checked against reality:

* **RPO** — how much data a recovery may lose. The manifest's `recovery_point` names the newest
  record in each backup, so the real exposure is `now - recovery_point`, which is the backup interval
  plus however long the last run took. Pick a cadence that keeps that below the agreed figure.
  *Suggestion:* a payments desk should back up at least every 15 minutes, and always immediately
  after an unattended `WORKER_AUTOPAY` run, because that is when new authorizations exist.
* **RTO** — how long a recovery may take. The only honest source is a measured drill:
  `restore_seconds` plus the time to bring the stack up and re-verify. Run the drill, and use what it
  reports rather than what seems plausible.

Record both, with the drill evidence, as the operator's own commitment. An unmeasured RTO is a wish.

## Restore drill

```sh
# Restore the newest backup into a disposable path and verify it
uv run arc-payables-restore-drill \
  --backup /srv/backups/arc-payables/arc-payables-20260101T020000Z.sqlite3 \
  --scratch /tmp/arc-payables-drill/restored.sqlite3
```

The drill copies the backup, verifies integrity, verifies the audit chain, compares row counts
against the manifest, and reports the measured restore time and the data checkpoint. It never
touches the live paths it is given, so it is safe to run on the production host.

**A backup that has never been restored is a belief, not a control.** Run this against the newest
backup on a schedule — monthly at minimum, and after any change to the backup destination, the hook,
or the database layout. A drill that fails is an incident, not a warning.

Verification here is **self-consistency**: each entry's signature is checked against the key that
entry names, which catches a rewritten row whose signature was not updated. Only the deployment's
own policy key can prove an entry came from *that* key, so the drill deliberately needs no HSM
access. For anchoring, run `arc-payables-reconcile` and the audit verification endpoint.

### Restoring for real

1. Stop the worker, then the API. Do not restore under a running writer.
2. Preserve the current database, including any `-wal`/`-shm` sidecars, even if it is the damaged
   one. It is evidence.
3. Restore the verified backup to the configured `DATABASE_PATH` **on the same release** as the code
   that will open it, and check `schema_migrations` before starting.
4. Start the API, then the worker. Keep `WORKER_AUTOPAY=false` until reconciliation is clean.
5. Run `arc-payables-reconcile`, then the audit verification. Anything that settled after the
   backup's `recovery_point` is *outside* the restored data and must be reconciled by hand against
   the chain — never by replaying an authorization.

**Never restore an older database over newer payment history**, and never restore to resolve a
reconciliation disagreement. Data that the backup does not contain still happened on chain.

## Alert delivery

```sh
uv run arc-payables-alert-test              # sends one clearly-labelled test alert
uv run arc-payables-alert-test --dry-run    # prints the payload, sends nothing
```

Exit codes: `0` delivered, `1` not delivered, `2` no destination configured. Alerts are recorded on
every worker pass whether or not a destination exists, so a failure here means *nobody was told*,
not that the worker is broken — the command says so explicitly, because the distinction changes what
an operator does next.

Proving delivery once is not a control either. The drill: run `arc-payables-alert-test` on the
cadence you agreed, and each time confirm a person received it. Delivery errors are also recorded on
the pass and shown in the Console's Worker view, so a silent webhook is visible in two places.

## Ownership

| Area | Named owner | Evidence the ownership is real |
| --- | --- | --- |
| Backup runs | Operations on-call | Scheduled runs succeed; failures page someone |
| Restore drills | Operations on-call | A dated drill report exists for the current month |
| Alert destination | Operations on-call | A person received the last `arc-payables-alert-test` |
| Reconciliation | Finance systems owner | Blocking findings are triaged and closed with a record |
| Policy key and guard | Security owner | Rotation and compromise procedures in [docs/signing.md](signing.md) have been walked through |
| Accounting correctness | Controller or accountant | PR 5 of this series: mappings approved by an accountant |

Fill this in before any real funds move. A table of role names is not ownership; a person who knows
they are on the hook is.

## Incident runbook

1. **Stop spending.** `WORKER_AUTOPAY=false`, or stop the worker. Reconcile or rotate before
   resuming. Nothing else is decided before this.
2. **Preserve.** Take a backup if the database is still readable; keep the sidecars, the logs and the
   raw chain data. Do not edit records to make a report pass.
3. **Classify.** `arc-payables-reconcile` separates a *proven* fault (blocking) from an *unresolved*
   question (review). They have different remedies; do not treat a review item as a funds fault, and
   never resend an unresolved payment on absence alone.
4. **Contain.** If a key is suspect, follow compromise recovery in [docs/signing.md](signing.md) and
   do **not** mark the compromised address as retired.
5. **Recover.** Restore only if data is lost, using the newest verified backup, and reconcile
   everything after its `recovery_point` by hand.
6. **Close.** Reconciliation clean, audit chain verified, alerts delivered, and a written record of
   what happened and what changed as a result.

## Limits of this deployment

Do not scale the worker to multiple replicas, and do not move the database to a shared filesystem.
SQLite allows one writer, and the worker loop is not a distributed queue. A multi-host deployment
needs a server database, a complete store implementation behind the existing boundary, and tested
transaction semantics for authorization and worker claims — that is a separate project, not a
Compose change. See [deploy/arc-payables/README.md](../deploy/arc-payables/README.md).
