# Streamlit ops console. Talks to the API only.
#   docker build -f dashboard/Dockerfile -t settleagent/dashboard .
FROM python:3.12-slim AS builder
ENV UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
RUN pip install --no-cache-dir uv==0.13.0
WORKDIR /app
COPY dashboard/pyproject.toml dashboard/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY dashboard/ops_dashboard ./ops_dashboard
COPY dashboard/app.py ./
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" PORT=8501
RUN useradd --system --uid 10001 --create-home app
WORKDIR /app
COPY --from=builder /app /app
USER 10001
EXPOSE 8501
CMD ["sh", "-c", "streamlit run app.py --server.address 0.0.0.0 --server.port ${PORT} --server.headless true --browser.gatherUsageStats false"]
