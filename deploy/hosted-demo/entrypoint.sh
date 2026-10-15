#!/usr/bin/env sh
# Create a disposable demo database, fill it, then serve the console and API.
#
# HOSTED_DEMO=1 is set in the image. It makes the process refuse to start when a live provider or
# credential is present, so this entrypoint never runs beside a live path. The database lives under
# /tmp, which the host wipes on restart, so the data is disposable by construction.
set -eu

mkdir -p "$(dirname "$DATABASE_PATH")" "$BACKUP_DIRECTORY"
arc-payables-seed
# One mock pass fills the settlement log with a simulated payment. A failed pass must not take the
# console down; it only leaves the screens emptier.
arc-payables-worker --once --autopay --intake || echo "hosted demo: the seed pass did not finish cleanly" >&2

exec uvicorn arc_payables.api:app --host 0.0.0.0 --port "${PORT:-8000}"
