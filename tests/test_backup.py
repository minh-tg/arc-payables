"""Backups and restore drills.

The tests concentrate on the two ways a backup programme fails while appearing to work: a copy
taken from a live database that is quietly torn, and a backup that has never been restored. The
first is tested by writing to the database *during* the backup and requiring the copy to verify.
The second is tested by restoring and comparing row counts, and by requiring the drill to fail on
a backup that has been altered after the manifest recorded its checksum.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from arc_payables.backup import (
    BackupError,
    create_backup,
    prune_backups,
    restore_drill,
    verify_database,
)
from arc_payables.currency import USDCOnlyConverter
from arc_payables.mock_adapters import MockAccountingConnector, MockPaymentProvider
from arc_payables.policy import DeterministicPolicy
from arc_payables.security import EIP712PermitSigner
from arc_payables.seed import seed_demo
from arc_payables.service import APWorkflow
from arc_payables.settings import Settings
from arc_payables.store import SQLiteEvidenceStore

POLICY_KEY = "0x" + "11" * 32


def _live_database(tmp_path: Path, *, settle: bool = True) -> tuple[SQLiteEvidenceStore, str]:
    """A real database with a signed audit chain, and optionally a settled payment."""
    settings = Settings(_env_file=None, database_path=tmp_path / "live.sqlite3")
    store = SQLiteEvidenceStore(settings.database_path)
    store.initialize()
    legitimate_id, _ = seed_demo(store)
    if not settle:
        store.set_audit_signer(EIP712PermitSigner(POLICY_KEY))
        return store, legitimate_id
    signer = EIP712PermitSigner(POLICY_KEY)
    store.set_audit_signer(signer)
    workflow = APWorkflow(
        store,
        MockAccountingConnector(store),
        MockPaymentProvider(store, signer=signer),
        signer,
        DeterministicPolicy(settings, USDCOnlyConverter("USD")),
        settings,
    )
    workflow.evaluate(legitimate_id)
    workflow.submit_payment(legitimate_id)
    return store, legitimate_id


def _manifest_for(path: Path) -> dict:
    return json.loads((path.parent / f"{path.stem}.manifest.json").read_text(encoding="utf-8"))


def test_a_backup_verifies_and_records_how_current_its_data_is(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")

    assert result.path.exists()
    manifest = _manifest_for(result.path)
    assert manifest["database"]["integrity_check"] == "ok"
    assert manifest["database"]["audit"]["ok"] is True
    # The RPO input: what point in time the copy actually reaches, read from the copy.
    assert manifest["database"]["recovery_point"]["latest_audit_events"]
    assert manifest["sha256"] == result.manifest["sha256"]
    assert manifest["database"]["counts"]["audit_events"] > 0
    # A backup holds the audit history and the credentials hash tables; keep it private.
    assert (result.path.stat().st_mode & 0o077) == 0


def test_a_backup_taken_while_a_writer_commits_is_not_torn(tmp_path):
    """The failure a file copy cannot avoid: SQLite writes during the copy.

    A torn copy would show up as a broken hash chain, a failed integrity check or missing rows,
    so writing throughout the backup and then verifying is the discriminating test.
    """
    store, _ = _live_database(tmp_path)
    stop = threading.Event()
    writes: list[int] = []

    def writer() -> None:
        settings = Settings(_env_file=None, database_path=store.path)
        signer = EIP712PermitSigner(POLICY_KEY)
        inner = SQLiteEvidenceStore(store.path)
        inner.initialize()
        workflow = APWorkflow(
            inner,
            MockAccountingConnector(inner),
            MockPaymentProvider(inner, signer=signer),
            signer,
            DeterministicPolicy(settings, USDCOnlyConverter("USD")),
            settings,
        )
        while not stop.is_set():
            try:
                invoice_id, _ = seed_demo(inner)
                workflow.evaluate(invoice_id)
                writes.append(1)
            except Exception:
                # Concurrent writers on one SQLite file can legitimately lose a race; the point
                # here is that the *backup* stays consistent while they try.
                writes.append(0)
            time.sleep(0.001)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    time.sleep(0.05)
    try:
        result = create_backup(store.path, tmp_path / "backups")
    finally:
        stop.set()
        thread.join(timeout=10)

    assert writes, "the writer must actually have run during the backup"
    report = verify_database(result.path, deep=True)
    assert report["ok"] is True, report
    assert report["audit"]["ok"] is True


def test_the_drill_restores_and_times_a_backup(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")

    report = restore_drill(result.path, tmp_path / "scratch" / "restored.sqlite3")
    assert report["ok"] is True
    assert report["restore_seconds"] >= 0
    assert report["database"]["audit"]["entries"] > 0
    assert report["database"]["counts"] == result.manifest["database"]["counts"]
    # The restored copy is a working database: it can be read by the real store.
    restored = SQLiteEvidenceStore(tmp_path / "scratch" / "restored.sqlite3")
    assert restored.verify_audit_chain()["ok"] is True


def test_the_drill_refuses_a_backup_whose_contents_changed_after_it_was_written(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")

    with sqlite3.connect(result.path) as connection:
        connection.execute("UPDATE audit_events SET payload_json='{}' WHERE id=1")
        connection.commit()

    with pytest.raises(BackupError, match="checksum"):
        restore_drill(result.path, tmp_path / "scratch" / "restored.sqlite3")


def test_a_tampered_chain_is_detected_inside_the_backup_itself(tmp_path):
    """Verification must read the copy, not trust that the copy is the live file."""
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")

    with sqlite3.connect(result.path) as connection:
        connection.execute("UPDATE audit_events SET event_hash=? WHERE id=1", ("0x" + "77" * 32,))
        connection.commit()

    report = verify_database(result.path, deep=True)
    assert report["ok"] is False
    assert report["audit"]["ok"] is False


def test_a_backup_is_not_published_when_it_cannot_be_verified(tmp_path, monkeypatch):
    """A partial or unverifiable copy must be removed, not left looking like a backup."""
    from arc_payables import backup as backup_module

    store, _ = _live_database(tmp_path)
    monkeypatch.setattr(
        backup_module, "verify_database", lambda path, deep=True: {"ok": False, "integrity_check": "corrupt"}
    )
    with pytest.raises(BackupError, match="did not verify"):
        create_backup(store.path, tmp_path / "backups")
    leftovers = list((tmp_path / "backups").iterdir())
    assert leftovers == [], f"nothing may be published from a failed run, found {leftovers}"


def test_a_failing_off_host_hook_fails_the_run(tmp_path):
    """A backup that stayed on the host is not an off-host backup, and must say so."""
    store, _ = _live_database(tmp_path)
    with pytest.raises(BackupError, match="NOT shipped"):
        create_backup(store.path, tmp_path / "backups", hook="false")
    # The verified local copy is kept, because it is still useful, and the failure is reported.
    assert list((tmp_path / "backups").glob("*.sqlite3"))
    manifest = json.loads(next((tmp_path / "backups").glob("*.manifest.json")).read_text(encoding="utf-8"))
    assert manifest["hook"]["returncode"] != 0


def test_a_working_hook_is_recorded_in_the_manifest(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups", hook="true")
    assert result.manifest["hook"]["returncode"] == 0


def test_retention_keeps_the_newest_and_never_deletes_an_unpaired_file(tmp_path):
    store, _ = _live_database(tmp_path)
    destination = tmp_path / "backups"
    stamps = ["20260101T000000Z", "20260102T000000Z", "20260103T000000Z"]
    for stamp in stamps:
        create_backup(
            store.path,
            destination,
            now=lambda stamp=stamp: datetime.strptime(stamp, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc),
        )
    # A stray copy with no manifest is somebody else's problem, not this tool's to delete.
    stray = destination / "arc-payables-20250101T000000Z.sqlite3"
    stray.write_bytes(b"")

    removed = prune_backups(destination, 2)
    assert removed == [f"arc-payables-{stamps[0]}.sqlite3"]
    remaining = sorted(path.name for path in destination.glob("*.sqlite3"))
    # The two newest are kept, the oldest is gone with its manifest, and the unpaired file that
    # was never written by this tool is left exactly where it was.
    assert remaining == [
        "arc-payables-20250101T000000Z.sqlite3",
        f"arc-payables-{stamps[1]}.sqlite3",
        f"arc-payables-{stamps[2]}.sqlite3",
    ]
    assert not (destination / f"arc-payables-{stamps[0]}.manifest.json").exists()
    assert (destination / "arc-payables-20250101T000000Z.sqlite3").exists()


def test_creating_a_backup_twice_in_the_same_second_is_refused(tmp_path):
    """Overwriting a backup would destroy the only copy of a recovery point."""
    store, _ = _live_database(tmp_path)
    fixed = lambda: datetime(2026, 1, 1, tzinfo=timezone.utc)  # noqa: E731
    create_backup(store.path, tmp_path / "backups", now=fixed)
    with pytest.raises(BackupError, match="already exists"):
        create_backup(store.path, tmp_path / "backups", now=fixed)


def test_a_table_that_does_not_exist_is_absent_not_empty(tmp_path):
    """A missing table must never be reported as zero rows, which reads as data loss."""
    from arc_payables.backup import COUNTED_TABLES

    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")
    counts = result.manifest["database"]["counts"]
    assert set(counts) == set(COUNTED_TABLES)
    # The fixture settles a payment, so these carry real rows from the real table names.
    assert counts["invoices"] >= 2
    assert counts["payments"] == 1
    assert counts["audit_events"] > 0
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE IF EXISTS payment_plans")
        connection.commit()
    dropped = create_backup(
        store.path,
        tmp_path / "backups2",
        now=lambda: datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    assert dropped.manifest["database"]["counts"]["payment_plans"] is None


def test_a_failing_hook_leaves_the_previous_backup_alone(tmp_path):
    """Pruning before shipping can delete the last good copy while the new one never leaves."""
    store, _ = _live_database(tmp_path)
    destination = tmp_path / "backups"
    keep_me = create_backup(store.path, destination, label="older",
                            now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))

    with pytest.raises(BackupError, match="NOT shipped"):
        create_backup(store.path, destination, keep=1, label="newer",
                      now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc), hook="false")

    assert keep_me.path.exists(), "the previous backup was pruned even though nothing shipped"
    assert (destination / f"{keep_me.path.stem}.manifest.json").exists()


def test_a_hook_that_never_returns_is_abandoned_and_reported(tmp_path):
    """A hung hook must not hold the backup run open forever.

    The hook always receives the backup path as its argument, so a hook that ignores extra
    arguments is the realistic case: a script that blocks after copying.
    """
    store, _ = _live_database(tmp_path)
    slow = tmp_path / "slow-hook.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    slow.chmod(0o755)

    with pytest.raises(BackupError, match="did not finish within"):
        create_backup(store.path, tmp_path / "backups", hook=str(slow), hook_timeout_seconds=1)
    # The verified local copy is kept and the manifest records what happened.
    manifest = json.loads(next((tmp_path / "backups").glob("*.manifest.json")).read_text(encoding="utf-8"))
    assert manifest["hook"]["returncode"] is None
    assert "did not finish" in manifest["hook"]["error"]


def _process_state(pid: int) -> str | None:
    """Linux process state: 'Z' for a zombie, None when the pid is gone entirely."""
    try:
        with open(f"/proc/{pid}/stat", encoding="utf-8") as handle:
            return handle.read().rsplit(") ", 1)[1].split()[0]
    except (FileNotFoundError, IndexError):
        return None


def test_a_timed_out_hook_does_not_leave_its_forks_running(tmp_path):
    """Killing only the direct child would leave a forked child holding the destination.

    A hook that backgrounds work and then blocks is the realistic shape. The whole process group
    must die, so the forked sleep is either gone or a reaped zombie rather than still running.
    """
    if not os.path.isdir("/proc"):
        pytest.skip("needs /proc to observe process state")
    store, _ = _live_database(tmp_path)
    pidfile = tmp_path / "child.pid"
    slow = tmp_path / "forking-hook.sh"
    slow.write_text(
        f"#!/bin/sh\nsleep 30 &\necho $! > {pidfile}\nsleep 30\n",
        encoding="utf-8",
    )
    slow.chmod(0o755)

    with pytest.raises(BackupError, match="did not finish within"):
        create_backup(store.path, tmp_path / "backups", hook=str(slow), hook_timeout_seconds=1)

    deadline = time.time() + 5
    while time.time() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.05)
    assert pidfile.exists() and pidfile.read_text().strip(), "the hook never reported its child"
    child = int(pidfile.read_text().strip())

    deadline = time.time() + 5
    state = _process_state(child)
    while time.time() < deadline and state not in (None, "Z"):
        time.sleep(0.1)
        state = _process_state(child)
    if state not in (None, "Z"):  # do not leak a process into the rest of the suite
        os.kill(child, 9)
    assert state in (None, "Z"), f"the forked child outlived the timeout in state {state!r}"


def test_a_successful_hook_still_prunes(tmp_path):
    """The ordering fix must not disable retention."""
    store, _ = _live_database(tmp_path)
    destination = tmp_path / "backups"
    create_backup(store.path, destination, label="older",
                  now=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    newest = create_backup(store.path, destination, keep=1, label="newer", hook="true",
                           now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc))
    remaining = sorted(path.stem for path in destination.glob("*.sqlite3"))
    assert remaining == [newest.path.stem]


def test_the_drill_refuses_to_restore_onto_the_live_database(tmp_path):
    """One typo in --scratch must not destroy the evidence the backup exists to protect."""
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")
    with pytest.raises(BackupError, match="live database"):
        restore_drill(result.path, store.path, live_database=store.path)


def test_the_drill_refuses_an_existing_target_unless_forced(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")
    scratch = tmp_path / "scratch" / "restored.sqlite3"
    scratch.parent.mkdir(parents=True)
    scratch.write_bytes(b"something already here")

    with pytest.raises(BackupError, match="already exists"):
        restore_drill(result.path, scratch)
    assert scratch.read_bytes() == b"something already here", "the existing file was touched"

    forced = restore_drill(result.path, scratch, force=True)
    assert forced["ok"] is True


def test_retention_follows_the_recorded_time_not_the_file_name(tmp_path):
    """Name order and recorded order can disagree; the manifest is the authority."""
    from arc_payables.backup import prune_backups

    store, _ = _live_database(tmp_path)
    destination = tmp_path / "backups"
    first = create_backup(store.path, destination,
                          now=lambda: datetime(2026, 1, 2, tzinfo=timezone.utc))
    second = create_backup(store.path, destination,
                           now=lambda: datetime(2026, 1, 3, tzinfo=timezone.utc))
    # Make the recorded times disagree with the file names: "first" now claims to be newer.
    manifest_path = destination / f"{first.path.stem}.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["created_at"] = datetime(2026, 1, 4, tzinfo=timezone.utc).isoformat()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    removed = prune_backups(destination, 1)
    assert removed == [second.path.name], "retention followed the file name, not the recorded time"
    assert first.path.exists()


def test_a_missing_source_is_reported_not_created(tmp_path):
    with pytest.raises(BackupError, match="does not exist"):
        create_backup(tmp_path / "absent.sqlite3", tmp_path / "backups")


def test_the_drill_reports_a_nameable_recovery_point(tmp_path):
    store, _ = _live_database(tmp_path)
    result = create_backup(store.path, tmp_path / "backups")
    report = restore_drill(result.path, tmp_path / "scratch" / "restored.sqlite3")
    assert report["checkpoint"]["latest_audit_events"]
    assert report["manifest_created_at"]
