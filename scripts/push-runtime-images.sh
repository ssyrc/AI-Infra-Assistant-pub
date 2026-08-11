#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DOCKERHUB_NS="${DOCKERHUB_NS:-ellie0}"
TAG="${TAG:-main-$(git rev-parse --short HEAD)}"

tag_and_push() {
  local src="$1"
  local repo="$2"
  local remote_tag="${DOCKERHUB_NS}/${repo}:${TAG}"
  local remote_latest="${DOCKERHUB_NS}/${repo}:latest"

  docker image inspect "$src" >/dev/null
  docker tag "$src" "$remote_tag"
  docker tag "$src" "$remote_latest"
  docker push "$remote_tag"
  docker push "$remote_latest"
}

tag_and_push ai-infra-assistant-db-init:latest ai-infra-assistant-db-init
tag_and_push ai-infra-assistant-mock-vllm:latest ai-infra-assistant-mock-vllm
tag_and_push ai-infra-assistant-mcp:dev ai-infra-assistant-mcp
tag_and_push ai-infra-assistant-agent-server:latest ai-infra-assistant-agent-server
tag_and_push ai-infra-assistant-admin-console:latest ai-infra-assistant-admin-console
tag_and_push pgvector/pgvector:pg16 ai-infra-assistant-pgvector
tag_and_push postgres:16-alpine ai-infra-assistant-postgres
tag_and_push ghcr.io/open-webui/open-webui:v0.6.5 ai-infra-assistant-open-webui

echo "pushed runtime images with tag: ${TAG}"
