#!/usr/bin/env bash
# dev 구성 빌드 + 기동.
# 폐쇄망 미러/프록시가 '동시 요청'에 간헐적으로 빈 응답(-> pip "from versions: none")을 줄 수 있으므로,
# 빌드 대상을 병렬로 몰지 않고 '한 서비스씩 순차' 빌드한다. 실패하면 재시도하지 않고 즉시 멈춘다.
#
# 사용법:
#   bash scripts/rebuild.sh            # 순차 빌드 후 기동
#   NOCACHE=1 bash scripts/rebuild.sh  # 캐시 없이 처음부터
set -u
cd "$(dirname "$0")/.."
COMPOSE="docker compose -f docker-compose.dev.yml"

if [ ! -f .env ]; then
  cp .env.example .env
else
  for key in APT_MIRROR APT_HTTP_TIMEOUT; do
    if ! grep -q "^${key}=" .env && grep -q "^${key}=" .env.example; then
      {
        echo
        grep "^${key}=" .env.example
      } >> .env
    fi
  done
fi

echo "== 사전 확인 =="
echo "vendor 휠:"
ls -1 vendor/*.whl 2>/dev/null || echo "(없음!)"
if grep -rq 'python:3.12' docker-compose*.yml ./*/Dockerfile* 2>/dev/null; then
  echo "!!! 아직 python:3.12 참조가 있다(3.11 이어야 함)"; fi

# 빌드 대상(= build: 있는 서비스). MCP 4개 서비스는 같은 이미지를 쓰므로 manual-mcp로 한 번만 빌드한다.
SERVICES="db-init mock-vllm manual-mcp agent-server admin-console"

if [ "${NOCACHE:-0}" = "1" ]; then
  echo "== 캐시 정리 =="; docker builder prune -af >/dev/null 2>&1 || true
fi

for svc in $SERVICES; do
  echo
  echo "======== 빌드: $svc ========"
  if COMPOSE_PARALLEL_LIMIT=1 $COMPOSE build ${NOCACHE:+--no-cache} "$svc"; then
    echo ">> $svc OK"
    continue
  fi
  echo
  echo "!!! 빌드 실패: $svc"
  echo "→ 재시도하지 않고 중단한다. 위 로그의 패키지가 미러에 없으면 해당 whl을 vendor/에 추가한 뒤 다시 실행."
  echo "→ 개별 재실행: $COMPOSE build $svc"
  exit 1
done

echo; echo "== 기동 =="
$COMPOSE up -d
$COMPOSE ps
echo "== health =="; sleep 3
curl -sS "http://localhost:${AGENT_PORT:-8500}/health" || true
echo
