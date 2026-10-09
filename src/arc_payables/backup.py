"""Consistent backups of the live database, and a restore drill that proves one is usable.

A backup that has never been restored is a belief, not a control. This module therefore has two
halves that are meant to be used together:

* ``create_backup`` copies the live database using SQLite's *online backup* API, so a running
  writer cannot tear the copy, then verifies the copy before it is published.
* ``restore_drill`` restores a backup into a scratch location, verifies it, and reports how long
  that took. That measured restore time is the only honest source of an RTO figure.

Two rules shape the design:

* **Nothing is published unverified.** The backup is written under a temporary name and only
  renamed into place after ``PRAGMA integrity_check`` passes, the audit chain verifies and the
  recorded signatures are self-consistent. A partial or torn file is removed, not kept.
* **Reading the database is not writing to it.** The source is opened read-only, and the drill
  never touches the live paths it is given.

The audit-chain check here is deliberately described as *self-consistent* rather than *anchored*:
it proves each entry's signature matches the key that entry names, which catches a rewritten row
whose signature was not updated. Only the deployment's own policy key can prove an entry came from
*that* key, and a backup tool must not need to open the HSM to read its own data.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .security import recover_digest_signer

MANIFEST_SUFFIX = ".manifest.json"
BACKUP_SUFFIX = ".sqlite3"
#: Tables whose row counts are compared after a restore. A restore that silently loses rows is
#: the failure this list exists to catch. The name is the real one from the migrations.
COUNTED_TABLES = (
    "invoices",
    "payments",
    "audit_events",
    "worker_runs",
    "screenings",
    "payment_plans",
    "receivables",
    "auth_sessions",
)


class BackupError(RuntimeError):
    """The backup could not be produced or verified, so it was not published."""


def _utc_stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_only(path: Path) -> sqlite3.Connection:
    """Open the source read-only: a backup run must not be able to change the live database."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    return connection


def _table_counts(connection: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        try:
            counts[table] = int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        except sqlite3.Error:
            # A deployment older than a migration legitimately lacks a table. It is reported as
            # absent rather than as zero rows, which would look like data loss.
            counts[table] = None
    return counts


def _recovery_point(connection: sqlite3.Connection) -> dict[str, Any]:
    """How current the data in this file is: the newest thing it knows about.

    This is the RPO input. "The backup ran at 02:00" says nothing about whether the data in it
    reaches 01:59 or 00:12, so the newest recorded timestamps are read from the copy itself.
    """
    point: dict[str, Any] = {}
    for table, column in (("audit_events", "created_at"), ("payments", "updated_at"), ("invoices", "updated_at")):
        try:
            row = connection.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
            point[f"latest_{table}"] = row[0] if row else None
        except sqlite3.Error:
            point[f"latest_{table}"] = None
    return point


def data_reaches(recovery_point: dict[str, Any] | None) -> str | None:
    """The newest recorded timestamp in a recovery point: how far the copy's data goes."""
    stamps = [value for value in (recovery_point or {}).values() if value]
    return max(stamps) if stamps else None


def _audit_self_consistency(connection: sqlite3.Connection) -> dict[str, Any]:
    """Verify the hash chain, and that each signature was made by the key the row names.

    Key-free on purpose: a backup must be verifiable without the signing key. This cannot prove
    the key is the *right* one; `arc-payables-reconcile` and the audit verification endpoint are
    where the deployment's own key is applied.
    """
    try:
        rows = connection.execute(
            "SELECT id,prev_hash,event_hash,signature,signer FROM audit_events ORDER BY id"
        ).fetchall()
    except sqlite3.Error as exc:
        return {"ok": False, "reason": f"audit_events unreadable: {type(exc).__name__}"}
    genesis = "0x" + "00" * 32
    expected_prev = genesis
    signed = 0
    for row in rows:
        if row["event_hash"] is None:
            continue
        if row["prev_hash"] != expected_prev:
            return {"ok": False, "reason": "prev_hash_does_not_match_the_previous_entry", "first_broken_id": row["id"]}
        if row["signature"]:
            signed += 1
            recovered = recover_digest_signer(bytes.fromhex(row["event_hash"][2:]), row["signature"])
            if recovered is None or recovered.lower() != (row["signer"] or "").lower():
                return {
                    "ok": False,
                    "reason": "signature_does_not_match_the_signer_recorded_on_the_entry",
                    "first_broken_id": row["id"],
                }
        expected_prev = row["event_hash"]
    return {"ok": True, "reason": None, "entries": len(rows), "signed_entries": signed, "head": expected_prev}


def verify_database(path: Path, *, deep: bool = True) -> dict[str, Any]:
    """Everything that can be checked about a database file without the signing key."""
    if not path.exists():
        raise BackupError(f"{path} does not exist")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        report: dict[str, Any] = {
            "integrity_check": integrity,
            "ok": integrity == "ok",
            "counts": _table_counts(connection),
            "recovery_point": _recovery_point(connection),
        }
        journal = connection.execute("PRAGMA journal_mode").fetchone()[0]
        report["journal_mode"] = journal
        if deep:
            report["audit"] = _audit_self_consistency(connection)
            report["ok"] = report["ok"] and bool(report["audit"]["ok"])
        return report
    except sqlite3.DatabaseError as exc:
        # A file that is not a readable database is a failed backup, not a crash: callers catch one error.
        raise BackupError(f"{path} is not a readable database: {exc}") from exc
    finally:
        connection.close()


@dataclass
class BackupResult:
    path: Path
    manifest_path: Path
    manifest: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": True, "backup": str(self.path), "manifest": str(self.manifest_path), **self.manifest}


def create_backup(
    database: Path | str,
    destination: Path | str,
    *,
    keep: int | None = None,
    label: str | None = None,
    hook: str | None = None,
    hook_timeout_seconds: int = 300,
    now: Callable[[], datetime] | None = None,
    verify: bool = True,
) -> BackupResult:
    """Copy the live database consistently, verify the copy, then publish it with a manifest.

    ``hook`` runs after the backup is published, with the backup path as its only argument, so an
    off-host copy or an encryption step can be plugged in without this tool pretending to move
    files itself. A failing hook makes the run fail: a backup that never left the host is not an
    off-host backup, and silence about that is the worst outcome.
    """
    source = Path(database)
    target_dir = Path(destination)
    if not source.exists():
        raise BackupError(f"database {source} does not exist")
    target_dir.mkdir(parents=True, exist_ok=True)
    moment = (now or (lambda: datetime.now(timezone.utc)))()
    stem = f"arc-payables-{_utc_stamp(moment)}" + (f"-{label}" if label else "")
    final_path = target_dir / f"{stem}{BACKUP_SUFFIX}"
    manifest_path = target_dir / f"{stem}{MANIFEST_SUFFIX}"
    temporary = target_dir / f".{stem}.partial"
    if final_path.exists() or manifest_path.exists():
        raise BackupError(f"a backup named {final_path.name} already exists; refusing to overwrite it")

    started = time.monotonic()
    try:
        with _read_only(source) as read_connection, sqlite3.connect(temporary) as write_connection:
            read_connection.backup(write_connection)
            # The copy inherits the source's WAL journal mode, which would make the artefact
            # ambiguous: a stray -wal file beside it would change what the backup appears to
            # contain while its checksum covered only the main file. A backup must be one
            # self-contained file, so the copy is switched to a rollback journal here.
            write_connection.execute("PRAGMA journal_mode=DELETE")
            write_connection.commit()
        # Verify the copy itself, before it is allowed to look like a backup.
        report = verify_database(temporary, deep=verify)
        if not report["ok"]:
            raise BackupError(f"the copy did not verify: {report.get('audit', {}).get('reason') or report['integrity_check']}")
        if report["journal_mode"].lower() != "delete":
            # A backup that is not a single file cannot be checksummed or copied faithfully.
            raise BackupError(f"the backup is not self-contained (journal_mode={report['journal_mode']})")
        temporary.replace(final_path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    elapsed = time.monotonic() - started

    manifest = {
        "created_at": moment.astimezone(timezone.utc).isoformat(),
        "source": str(source),
        "backup": final_path.name,
        "size_bytes": final_path.stat().st_size,
        "sha256": _sha256(final_path),
        "duration_seconds": round(elapsed, 3),
        "database": report,
        "label": label,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    manifest_path.chmod(0o600)
    final_path.chmod(0o600)

    if hook:
        # A hook that never returns must not hold the backup run open forever, and one that forks
        # must not leave children behind still holding the destination. It runs in its own process
        # group so the whole group can be killed, not just the process this code can see.
        process = subprocess.Popen(
            [*hook.split(), str(final_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            hook_stdout, hook_stderr = process.communicate(timeout=hook_timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            manifest["hook"] = {
                "command": hook,
                "returncode": None,
                "error": f"did not finish within {hook_timeout_seconds}s; its process group was killed",
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            raise BackupError(
                f"the off-host hook did not finish within {hook_timeout_seconds}s; the backup exists "
                f"locally at {final_path} but was NOT shipped, so this run is not a completed "
                "off-host backup"
            )
        manifest["hook"] = {"command": hook, "returncode": process.returncode}
        if process.returncode != 0:
            manifest["hook"]["error"] = (hook_stderr or hook_stdout or "")[:400]
            manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
            raise BackupError(
                f"the off-host hook failed ({process.returncode}); the backup exists locally at "
                f"{final_path} but was NOT shipped, so this run is not a completed off-host backup"
            )

    # Pruning runs only after a successful ship. Deleting an older copy before the new one has left
    # the host can leave the deployment with fewer off-host copies than intended: with keep=1 a
    # failing hook would remove the previous local copy while the new one never shipped.
    removed = prune_backups(target_dir, keep) if keep else []
    manifest["pruned"] = removed
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return BackupResult(path=final_path, manifest_path=manifest_path, manifest=manifest)


def _backup_files(directory: Path) -> list[Path]:
    return sorted(directory.glob(f"*{BACKUP_SUFFIX}"), key=lambda path: path.name)


def list_backups(directory: Path | str) -> list[dict[str, Any]]:
    """Backups in a directory, newest first, each with what its manifest recorded.

    A backup file with no readable manifest is listed as incomplete rather than hidden: that is
    the file an operator most needs to see.
    """
    folder = Path(directory)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(_backup_files(folder), key=_recorded_time, reverse=True):
        manifest_path = folder / f"{path.stem}{MANIFEST_SUFFIX}"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = None
        manifest = manifest if isinstance(manifest, dict) else {}
        database = manifest.get("database") if isinstance(manifest.get("database"), dict) else {}
        rows.append({
            "name": path.name,
            "complete": bool(manifest),
            "created_at": manifest.get("created_at"),
            "size_bytes": manifest.get("size_bytes"),
            "sha256": manifest.get("sha256"),
            "data_reaches": data_reaches(database.get("recovery_point")),
        })
    return rows


def _kill_process_group(process: subprocess.Popen) -> None:
    """Kill a hook and anything it forked, then reap it.

    The hook was started with ``start_new_session=True``, so its process group contains every child
    it spawned. Killing only the direct child would leave those children running, still holding the
    destination path this run just reported as unshipped.
    """
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        process.kill()
    try:
        process.communicate(timeout=5)
    except Exception:
        # Reaping is best effort: the timeout has already been reported to the operator.
        pass


def _recorded_time(path: Path) -> tuple[str, str]:
    """Sort key for retention: the manifest's own timestamp, then the file name."""
    manifest = path.parent / f"{path.stem}{MANIFEST_SUFFIX}"
    try:
        recorded = str(json.loads(manifest.read_text(encoding="utf-8")).get("created_at") or "")
    except (OSError, ValueError):
        recorded = ""
    return (recorded, path.name)


def prune_backups(directory: Path | str, keep: int) -> list[str]:
    """Delete all but the newest ``keep`` complete backups, with their manifests.

    Only a backup that has a manifest is pruned: a manifest-less file is somebody else's or an
    interrupted run, and deleting it would hide the problem instead of reporting it.
    """
    target = Path(directory)
    if keep <= 0:
        return []
    complete = [path for path in _backup_files(target) if (target / f"{path.stem}{MANIFEST_SUFFIX}").exists()]
    # Ordered by the time the backup records, not by its file name. Name order equals chronological
    # order only for the bare timestamp: a --label suffix sorts before the unlabeled name for the
    # same second, which would make a newer backup look older and prune it first.
    complete = sorted(complete, key=_recorded_time)
    removed: list[str] = []
    for path in complete[:-keep] if len(complete) > keep else []:
        manifest = target / f"{path.stem}{MANIFEST_SUFFIX}"
        path.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
        removed.append(path.name)
    return removed


def restore_drill(backup: Path | str, scratch: Path | str, *, now: Callable[[], datetime] | None = None,
                  live_database: Path | str | None = None, force: bool = False) -> dict[str, Any]:
    """Restore a backup into a scratch path and prove it is usable; never touch the live paths.

    Returns the measured restore time and the verification of the *restored* copy, because a
    backup that verifies but cannot be restored is not a backup. Any mismatch between the
    manifest's row counts and the restored copy is a failure, not a warning.
    """
    source = Path(backup)
    destination = Path(scratch)
    if not source.exists():
        raise BackupError(f"backup {source} does not exist")
    # A drill overwrites its destination, and it is meant to be run on the production host. Naming
    # the live database here would destroy the very evidence the backup exists to protect, so the
    # two dangerous destinations are refused rather than trusted to a docstring.
    if live_database is not None and destination.resolve() == Path(live_database).resolve():
        raise BackupError(
            "the drill target is the live database; restore into a disposable path instead. "
            "Overwriting it would destroy current records that no backup contains."
        )
    if destination.exists() and not force:
        raise BackupError(
            f"{destination} already exists; pass --force to overwrite it, or choose an empty path"
        )
    manifest_path = destination.parent / f"{source.stem}{MANIFEST_SUFFIX}"
    if not manifest_path.exists():
        manifest_path = source.parent / f"{source.stem}{MANIFEST_SUFFIX}"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    recorded_sha = manifest.get("sha256")
    if recorded_sha and _sha256(source) != recorded_sha:
        raise BackupError(
            "the backup file does not match the checksum in its manifest; the file changed after it "
            "was written and must not be trusted"
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    shutil.copy2(source, destination)
    report = verify_database(destination, deep=True)
    elapsed = time.monotonic() - started
    if not report["ok"]:
        raise BackupError(f"the restored copy did not verify: {report.get('audit', {}).get('reason') or report['integrity_check']}")

    expected = (manifest.get("database") or {}).get("counts") or {}
    mismatches = {
        table: {"manifest": expected[table], "restored": report["counts"].get(table)}
        for table in expected
        if expected[table] != report["counts"].get(table)
    }
    if mismatches:
        raise BackupError(f"the restored copy has different row counts than the manifest: {mismatches}")

    return {
        "ok": True,
        "backup": str(source),
        "restored_to": str(destination),
        "restore_seconds": round(elapsed, 3),
        "checked_at": (now or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc).isoformat(),
        "manifest_created_at": manifest.get("created_at"),
        "database": report,
        "checkpoint": manifest.get("database", {}).get("recovery_point"),
    }


def _format_report(report: dict[str, Any]) -> str:
    lines = [
        f"Restored {report['backup']} to {report['restored_to']} in {report['restore_seconds']}s",
        f"  integrity: {report['database']['integrity_check']}, journal_mode={report['database']['journal_mode']}",
        f"  audit chain: ok, {report['database']['audit']['entries']} entries "
        f"({report['database']['audit']['signed_entries']} signed, self-consistent)",
        "  row counts: "
        + ", ".join(
            f"{table}={count}"
            for table, count in sorted(report["database"]["counts"].items())
            if count is not None
        )
        + (
            "  (absent: "
            + ", ".join(sorted(t for t, c in report["database"]["counts"].items() if c is None))
            + ")"
            if any(c is None for c in report["database"]["counts"].values())
            else ""
        ),
        f"  data reaches: {report.get('checkpoint')}",
        f"  backup was taken: {report.get('manifest_created_at')}",
        "  Verification is self-consistency, not anchoring to the deployment key: run "
        "arc-payables-reconcile and the audit verification endpoint for that.",
    ]
    return "\n".join(lines)


def drill_main(argv: list[str] | None = None) -> int:
    """``arc-payables-restore-drill``: prove a backup restores, and time it.

    The measured time is the only defensible RTO number, and running it against the newest backup
    is the difference between having backups and being able to recover.
    """
    parser = argparse.ArgumentParser(description="Restore a backup into a scratch path and verify it")
    parser.add_argument("--backup", required=True, help="Backup file to restore")
    parser.add_argument(
        "--scratch",
        required=True,
        help="Where to restore. Use a disposable path: the file is overwritten each drill and the "
        "live database must never be named here.",
    )
    parser.add_argument("--force", action="store_true",
                        help="Overwrite the scratch path if it already exists")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .settings import get_settings

    try:
        report = restore_drill(args.backup, args.scratch, force=args.force,
                               live_database=get_settings().database_path)
    except BackupError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2, sort_keys=True) if args.json else _format_report(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up the live database consistently, verify the copy, and optionally ship it off-host"
    )
    parser.add_argument("--database", default=None, help="Source database (default: configured DATABASE_PATH)")
    parser.add_argument("--destination", required=True, help="Directory for the backup and its manifest")
    parser.add_argument("--keep", type=int, default=None, help="Keep only the newest N complete backups")
    parser.add_argument("--label", default=None, help="Short label appended to the file name")
    parser.add_argument(
        "--hook",
        default=None,
        help="Command run with the backup path after it is verified (for example: rclone copy or age). "
        "Its failure fails the run, because a backup that stayed on the host is not an off-host backup.",
    )
    parser.add_argument("--hook-timeout-seconds", type=int, default=300,
                        help="Give up on the off-host hook after this long (default: 300)")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    from .settings import get_settings

    database = Path(args.database) if args.database else Path(get_settings().database_path)
    try:
        result = create_backup(
            database, args.destination, keep=args.keep, label=args.label, hook=args.hook,
            hook_timeout_seconds=args.hook_timeout_seconds,
        )
    except BackupError as exc:
        print(f"FAIL  {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(f"Backup written: {result.path} ({result.manifest['size_bytes']} bytes)")
        print(f"  sha256: {result.manifest['sha256']}")
        print(f"  data reaches: {result.manifest['database']['recovery_point']}")
        if result.manifest.get("pruned"):
            print(f"  pruned: {', '.join(result.manifest['pruned'])}")
    return 0


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
