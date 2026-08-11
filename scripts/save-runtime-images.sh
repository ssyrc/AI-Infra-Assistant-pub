#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

TAG="${TAG:-main-$(git rev-parse --short HEAD)}"
OUT="${OUT:-dist/ai-infra-assistant-runtime-${TAG}.tar}"

mkdir -p "$(dirname "$OUT")"
docker save -o "$OUT" \
  ai-infra-assistant-db-init:latest \
  ai-infra-assistant-mock-vllm:latest \
  ai-infra-assistant-mcp:dev \
  ai-infra-assistant-agent-server:latest \
  ai-infra-assistant-admin-console:latest \
  pgvector/pgvector:pg16 \
  postgres:16-alpine \
  ghcr.io/open-webui/open-webui:v0.6.5

ls -lh "$OUT"
