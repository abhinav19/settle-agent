#!/usr/bin/env bash
# Start the four MCP tool servers (streamable HTTP), the agent/consumer, the API and the dashboard.
# Prereqs: `make up && make migrate && make seed`, and a populated .env (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

# Dashboard links trace ids to Jaeger unless you override the template.
export TRACE_URL_TEMPLATE="${TRACE_URL_TEMPLATE:-http://localhost:16686/trace/{trace_id}}"

pids=()
cleanup() {
  trap - EXIT INT TERM
  for p in "${pids[@]:-}"; do kill "$p" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup EXIT INT TERM

for spec in security_master:8101 ssi_store:8102 holiday_calendar:8103 trade_store:8104; do
  module="${spec%%:*}"
  port="${spec##*:}"
  (cd mcp-servers && SETTLE_MCP_TRANSPORT=streamable-http SETTLE_MCP_PORT="$port" \
    exec uv run python -m "${module}.server") &
  pids+=("$!")
done

(cd agent-core && exec uv run python -m settleagent.main) &
agent_pid=$!
pids+=("$agent_pid")

# Control-plane API (http://localhost:8000/docs) and ops dashboard (http://localhost:8501).
(cd agent-core && exec uv run uvicorn --factory settleagent.api.main:create_app_from_settings \
  --host 127.0.0.1 --port "${API_PORT:-8000}") &
pids+=("$!")

(cd dashboard && SETTLE_API_URL="${SETTLE_API_URL:-http://localhost:${API_PORT:-8000}}" \
  SETTLE_API_KEY="${SETTLE_API_KEY:-${API_KEY:-}}" \
  exec uv run streamlit run app.py --server.port "${DASHBOARD_PORT:-8501}" \
  --server.headless true --browser.gatherUsageStats false) &
pids+=("$!")

# Exit when the agent exits (SIGINT/SIGTERM reach it through the trap).
wait "$agent_pid"
