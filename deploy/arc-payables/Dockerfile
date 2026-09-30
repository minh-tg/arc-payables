FROM docker.io/library/python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /uvx /bin/

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY deploy/arc-payables/worker_healthcheck.py ./deploy/arc-payables/worker_healthcheck.py

RUN uv sync --frozen --no-dev \
    && install -d -o 10001 -g 10001 /app/data

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "arc_payables.api:app", "--host", "0.0.0.0", "--port", "8000"]
