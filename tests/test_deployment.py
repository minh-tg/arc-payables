"""Deployment files are operational code: validate their wiring and health checks."""

from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from arc_payables.store import SQLiteEvidenceStore

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "arc-payables"


def _healthcheck_module():
    path = DEPLOY / "worker_healthcheck.py"
    spec = importlib.util.spec_from_file_location("worker_healthcheck", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_runs_one_api_and_one_worker_with_shared_database():
    config = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    assert set(config["services"]) == {"api", "worker"}
    api = config["services"]["api"]
    worker = config["services"]["worker"]
    assert api["restart"] == worker["restart"] == "unless-stopped"
    assert api["ports"] == ["127.0.0.1:8000:8000"]
    assert api["volumes"] == worker["volumes"] == ["arc-payables-data:/app/data"]
    assert api["environment"]["DATABASE_PATH"] == worker["environment"]["DATABASE_PATH"]
    assert worker["depends_on"]["api"]["condition"] == "service_healthy"
    assert "--autopay" not in worker.get("command", [])
    assert "build" in api and "build" not in worker
    assert "../../.secret" in api["env_file"]
    assert api["env_file"] == worker["env_file"]


def test_compose_healthchecks_and_metrics_use_no_embedded_credentials():
    config = yaml.safe_load((DEPLOY / "docker-compose.yml").read_text())
    api_check = " ".join(config["services"]["api"]["healthcheck"]["test"])
    worker_check = " ".join(config["services"]["worker"]["healthcheck"]["test"])
    assert "/ready" in api_check
    assert "worker_healthcheck.py" in worker_check

    metrics = yaml.safe_load((DEPLOY / "prometheus.yml").read_text())
    scrape = metrics["scrape_configs"][0]
    assert scrape["metrics_path"] == "/metrics"
    assert scrape["authorization"]["credentials_file"] == "/run/secrets/arc-payables-api-key"
    assert "credentials" not in scrape["authorization"]


def test_docker_and_git_contexts_exclude_operational_notes_and_secret_files():
    docker_ignored = (ROOT / ".dockerignore").read_text().splitlines()
    git_ignored = (ROOT / ".gitignore").read_text().splitlines()
    assert "HANDOFF.md" in docker_ignored and "HANDOFF.md" in git_ignored
    assert ".secret" in docker_ignored and ".secret" in git_ignored
    assert "*.secret" in docker_ignored and "*.secret" in git_ignored


def test_worker_healthcheck_detects_missing_stale_and_recent_passes(tmp_path: Path, monkeypatch):
    healthcheck = _healthcheck_module()
    monkeypatch.setenv("WORKER_INTERVAL_SECONDS", "60")
    database = tmp_path / "health.sqlite3"
    assert not healthcheck.is_healthy(database)

    store = SQLiteEvidenceStore(database)
    store.initialize()
    now = datetime(2026, 10, 1, tzinfo=timezone.utc)
    store.record_worker_run(
        (now - timedelta(seconds=901)).isoformat(),
        (now - timedelta(seconds=900)).isoformat(),
        "ok",
        {},
    )
    assert healthcheck.is_healthy(database, now=now)

    store.record_worker_run(
        (now - timedelta(seconds=902)).isoformat(),
        (now - timedelta(seconds=901)).isoformat(),
        "ok",
        {},
    )
    assert not healthcheck.is_healthy(database, now=now)

    store.record_worker_run(
        (now - timedelta(seconds=2)).isoformat(),
        (now - timedelta(seconds=1)).isoformat(),
        "degraded",
        {},
    )
    assert healthcheck.is_healthy(database, now=now)
