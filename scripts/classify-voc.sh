#!/usr/bin/env bash
# VOC 처리 주체(사용자가 직접 한 건 / 운영자가 확인·조치한 건)를 LLM으로 판정해 저장한다.
#
# 왜 있나: 예전에는 검색할 때마다 답변 텍스트의 키워드로 추론했는데, 그 표현을 안 쓴 답변을
# 통째로 놓쳤다(#157). 목록을 늘리는 방식은 계속 뚫린다. 이제 LLM이 '사용자가 자기 권한으로
# 재현할 수 있는가'로 판정하고 결과를 `voc_records.handled_by`에 저장한다.
#
# 안전: 이미 판정된 행은 건드리지 않는다(`handled_by IS NULL`인 행만). 몇 번을 돌려도 된다.
#       중간에 끊어도 그때까지 판정된 것은 남는다.
#
# 사용법:
#   bash scripts/classify-voc.sh 20      # 먼저 20건만 — **품질을 눈으로 확인하고** 시작한다
#   bash scripts/classify-voc.sh         # 미분류 전부
#   bash scripts/classify-voc.sh 0 show  # 판정 결과 표본 20건을 보여주기만 한다(분류 안 함)
set -euo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
LIMIT="${1:-0}"
MODE="${2:-run}"

cd "$(dirname "${BASH_SOURCE[0]}")/.."

if ! docker compose -f "$COMPOSE_FILE" ps --status running --services 2>/dev/null \
      | grep -qx admin-console; then
  echo "[voc-classify] admin-console 컨테이너가 떠 있지 않습니다. 먼저 기동하세요:" >&2
  echo "               docker compose -f $COMPOSE_FILE up -d admin-console" >&2
  exit 1
fi

docker compose -f "$COMPOSE_FILE" exec -T \
  -e VOC_CLASSIFY_LIMIT="$LIMIT" -e VOC_CLASSIFY_MODE="$MODE" \
  admin-console python3 - <<'PY'
import asyncio
import os
import sys

sys.path.insert(0, "/app/shared")

from db import get_pool                     # noqa: E402
from voc_classify import classify_pending    # noqa: E402

LIMIT = int(os.environ.get("VOC_CLASSIFY_LIMIT") or 0)
MODE = os.environ.get("VOC_CLASSIFY_MODE") or "run"


async def counts(pool):
    return await pool.fetchrow(
        """
        SELECT count(*) AS total,
               count(*) FILTER (WHERE handled_by IS NULL) AS pending,
               count(*) FILTER (WHERE handled_by = 'user') AS c_user,
               count(*) FILTER (WHERE handled_by = 'operator') AS c_operator,
               count(*) FILTER (WHERE handled_by = 'unknown') AS c_unknown
        FROM voc_records
        """
    )


async def show(pool):
    rows = await pool.fetch(
        """
        SELECT id, handled_by, handled_by_reason, left(question, 60) AS q
        FROM voc_records WHERE handled_by IS NOT NULL
        ORDER BY handled_by_at DESC NULLS LAST, id DESC LIMIT 20
        """
    )
    if not rows:
        print("아직 판정된 행이 없습니다.")
        return
    print(f"\n{'id':>6}  {'판정':<9} 근거 / 문의")
    print("-" * 100)
    for r in rows:
        print(f"{r['id']:>6}  {r['handled_by']:<9} {r['handled_by_reason'] or ''}")
        print(f"        {'':<9} └ {(r['q'] or '').strip()}")


async def main():
    pool = await get_pool("voc_db_dsn")
    before = await counts(pool)
    print(f"전체 {before['total']}건 · 미분류 {before['pending']}건 "
          f"(user {before['c_user']} / operator {before['c_operator']} / "
          f"unknown {before['c_unknown']})")

    if MODE == "show":
        await show(pool)
        return
    if not before["pending"]:
        print("미분류가 없습니다. 할 일이 없습니다.")
        await show(pool)
        return

    def progress(done, total, classified, failed):
        print(f"  … {done}/{total}  판정 {classified}  실패 {failed}", flush=True)

    print(f"\n분류를 시작합니다{'(최대 %d건)' % LIMIT if LIMIT else ''}. "
          f"중간에 끊어도 그때까지는 저장됩니다.\n")
    result = await classify_pending(pool, limit=LIMIT, progress=progress)

    after = await counts(pool)
    print(f"\n완료: 판정 {result['classified']}건 · 실패 {result['failed']}건 "
          f"{result['counts']}")
    print(f"남은 미분류 {after['pending']}건")
    await show(pool)
    print("\n판정이 이상하면 그 id와 문의/답변을 알려주세요 — 판정 기준을 고칩니다.")


asyncio.run(main())
PY
