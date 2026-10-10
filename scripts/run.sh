#!/usr/bin/env bash
# Start the four MCP tool servers (streamable HTTP) and the agent/consumer.
# Prereqs: `make up && make migrate && make seed`, and a populated .env (see .env.example).
set -euo pipefail
cd "$(dirname "$0")/.."

if [ -f .env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

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

# Exit when the agent exits (SIGINT/SIGTERM reach it through the trap).
wait "$agent_pid"
