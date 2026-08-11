#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERHUB_NS="${DOCKERHUB_NS:-ellie0}"
TAG="${1:-${TAG:-}}"

if [ -z "$TAG" ]; then
  echo "usage: bash scripts/retag-runtime-images.sh <runtime-image-tag>" >&2
  echo "example: bash scripts/retag-runtime-images.sh main-10bc550" >&2
  exit 2
fi

tag_existing() {
  local repo="$1"
  local local_tag="$2"
  local remote="${DOCKERHUB_NS}/${repo}:${TAG}"

  if ! docker image inspect "$remote" >/dev/null 2>&1; then
    echo "missing image: $remote" >&2
    echo "pull or load it first, then rerun this script." >&2
    echo "expected source image: $remote" >&2
    exit 1
  fi

  docker tag "$remote" "$local_tag"
  echo "$remote -> $local_tag"
}

tag_existing ai-infra-assistant-db-init ai-infra-assistant-db-init:latest
tag_existing ai-infra-assistant-mock-vllm ai-infra-assistant-mock-vllm:latest
tag_existing ai-infra-assistant-mcp ai-infra-assistant-mcp:dev
tag_existing ai-infra-assistant-agent-server ai-infra-assistant-agent-server:latest
tag_existing ai-infra-assistant-admin-console ai-infra-assistant-admin-console:latest
tag_existing ai-infra-assistant-pgvector pgvector/pgvector:pg16
tag_existing ai-infra-assistant-postgres postgres:16-alpine
tag_existing ai-infra-assistant-open-webui ghcr.io/open-webui/open-webui:v0.6.5

echo "retagged runtime images for docker-compose.dev.yml: ${TAG}"
