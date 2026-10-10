# Agent + API image (one image, two commands). Build context: the repository root.
#   docker build -f agent-core/Dockerfile -t settleagent/agent-core .
FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.13.0
WORKDIR /app
COPY agent-core/pyproject.toml agent-core/uv.lock ./
# dependencies first (cached layer); --no-dev skips the test/dev tooling and the path-linked mcp-servers
RUN uv sync --frozen --no-dev --no-install-project
COPY agent-core/src ./src
COPY agent-core/alembic.ini ./
COPY agent-core/alembic ./alembic
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH"
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=builder /app /app
USER 10001
EXPOSE 8000
# Default: the Kafka consumer + agent. The API runs the same image with a different command:
#   uvicorn --factory settleagent.api.main:create_app_from_settings --host 0.0.0.0 --port 8000
CMD ["python", "-m", "settleagent.main"]
