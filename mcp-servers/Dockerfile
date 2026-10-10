# The four read-only MCP tool servers (one image; the command picks the server).
#   docker build -f mcp-servers/Dockerfile -t settleagent/mcp-servers .
FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.13.0
WORKDIR /app
COPY mcp-servers/pyproject.toml mcp-servers/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY mcp-servers/_shared ./_shared
COPY mcp-servers/security_master ./security_master
COPY mcp-servers/ssi_store ./ssi_store
COPY mcp-servers/holiday_calendar ./holiday_calendar
COPY mcp-servers/trade_store ./trade_store
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    SETTLE_DATA_DIR=/data/synthetic \
    SETTLE_MCP_TRANSPORT=streamable-http \
    SETTLE_MCP_HOST=0.0.0.0
RUN useradd --system --uid 10001 --no-create-home app
WORKDIR /app
COPY --from=builder /app /app
COPY data/synthetic /data/synthetic
USER 10001
# e.g.  python -m trade_store.server     (port from SETTLE_MCP_PORT, default per server)
CMD ["python", "-m", "trade_store.server"]
