#!/usr/bin/env python3
"""Build both release archives and smoke-test the extracted wheel without checkout assets."""
from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]


def validate_members(names, required=()):
    if len(names) != len(set(names)):
        raise ValueError("Duplicate archive members")
    missing = set(required) - set(names)
    if missing:
        raise ValueError(f"Missing runtime assets: {sorted(missing)}")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe archive path: {name}")
        if any(part in {"local-docs", "data", "recovery", "HANDOFF.md"}
               or part.startswith(".env") or part.endswith((".secret", ".key", ".pem", ".sqlite3"))
               for part in path.parts):
            raise ValueError(f"Private artifact in release: {name}")


def main():
    with tempfile.TemporaryDirectory(prefix="arc-package-check-") as work:
        work = Path(work)
        dist = work / "dist"
        subprocess.run(["uv", "build", "--out-dir", str(dist)], cwd=ROOT, check=True)
        wheels = list(dist.glob("*.whl"))
        sources = list(dist.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sources) != 1:
            raise ValueError("Expected exactly one wheel and source archive")
        assets = {
            "arc_payables/" + str(path.relative_to(ROOT / "src/arc_payables"))
            for path in (ROOT / "src/arc_payables/web").rglob("*") if path.is_file()
        }
        migrations = {
            "arc_payables/migrations/" + path.name
            for path in (ROOT / "migrations").glob("*.sql")
        }
        extracted = work / "wheel"
        with zipfile.ZipFile(wheels[0]) as archive:
            validate_members(archive.namelist(), assets | migrations)
            archive.extractall(extracted)
        with tarfile.open(sources[0]) as archive:
            names = archive.getnames()
            validate_members(names)
            allowed = {".gitignore", "LICENSE", "PKG-INFO", "README.md",
                       "migrations", "pyproject.toml", "src", "uv.lock"}
            if any(len(PurePosixPath(name).parts) < 2
                   or PurePosixPath(name).parts[1] not in allowed for name in names):
                raise ValueError("Source archive escaped its publication allowlist")
            if any(member.issym() or member.islnk() for member in archive):
                raise ValueError("Release archive contains a link")
        smoke = """
from pathlib import Path
import arc_payables
from arc_payables.api import app
from fastapi.testclient import TestClient
assert Path(arc_payables.__file__).resolve().is_relative_to(Path("wheel").resolve())
with TestClient(app) as client:
    assert client.get("/health").status_code == 200
    assert client.get("/ready").status_code == 200
    console = client.get("/console/")
    assert console.status_code == 200 and "operator console" in console.text
    assert client.get("/console/app.js").status_code == 200
    assert client.get("/explanations.json").status_code == 200
    assert client.post("/demo/start").status_code == 200
    assert client.get("/payments").json()["count"] == 0
print("Extracted wheel: migrations, console assets and mock APIs passed")
"""
        env = {
            "PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
            "PYTHONPATH": str(extracted), "ENVIRONMENT": "local", "AUTH_MODE": "demo",
            "PAYMENT_PROVIDER": "mock", "ACCOUNTING_PROVIDER": "mock",
            "SCREENING_PROVIDER": "fixture", "DECISION_LAYER": "heuristics",
            "SIGNER_BACKEND": "env", "API_KEY": "", "APPROVAL_TOKEN": "",
            "DATABASE_PATH": str(work / "demo.sqlite3"), "WORKER_AUTOPAY": "false",
        }
        subprocess.run([sys.executable, "-c", smoke], cwd=work, env=env, check=True)
        print(f"Release archives verified: {len(assets)} web assets, {len(migrations)} migrations; no private paths")


if __name__ == "__main__":
    main()
