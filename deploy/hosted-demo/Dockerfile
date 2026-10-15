# Hosted mock demo. Public, disposable, and it refuses to start with a live provider or secret.
# Build from the repository root:
#   podman build -f deploy/hosted-demo/Dockerfile -t arc-payables-hosted-demo .
FROM docker.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOSTED_DEMO=1 \
    ENVIRONMENT=local \
    AUTH_MODE=demo \
    PAYMENT_PROVIDER=mock \
    ACCOUNTING_PROVIDER=mock \
    SCREENING_PROVIDER=fixture \
    DECISION_LAYER=heuristics \
    SIGNER_BACKEND=env \
    API_KEY= \
    APPROVAL_TOKEN= \
    WORKER_AUTOPAY=false \
    MIN_RESERVE_USDC=2000 \
    MAX_INVOICE_USDC=1000 \
    CRITICAL_SUPPLIER_IDS=[] \
    DATABASE_PATH=/tmp/arc-payables/demo.sqlite3 \
    BACKUP_DIRECTORY=/tmp/arc-payables/backups

COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY deploy/hosted-demo/entrypoint.sh ./deploy/hosted-demo/entrypoint.sh

RUN uv sync --frozen --no-dev \
    && chmod +x deploy/hosted-demo/entrypoint.sh

USER 10001:10001
EXPOSE 8000

ENTRYPOINT ["/app/deploy/hosted-demo/entrypoint.sh"]
