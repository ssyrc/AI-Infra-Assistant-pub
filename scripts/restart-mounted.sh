#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

COMPOSE="docker compose -f docker-compose.dev.yml"
SERVICES="agent-server manual-mcp execution-mcp voc-mcp chart-mcp admin-console mock-vllm"

echo "== restart mounted-code services =="
$COMPOSE restart $SERVICES
$COMPOSE ps

echo "== health =="
for _ in {1..30}; do
  if curl -fsS "http://localhost:${AGENT_PORT:-8500}/health"; then
    echo
    exit 0
  fi
  sleep 1
done

echo "agent health check failed" >&2
exit 1
