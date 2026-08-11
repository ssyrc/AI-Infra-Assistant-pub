#!/usr/bin/env bash
# DB에 저장된 지시문이 **지금 코드의 것과 같은지** 확인한다 (#151, #152).
#
# 왜 스크립트인가: 그동안 "지시문에 이 문구가 있으면 최신"처럼 **매직 문자열**로 확인했다.
# 지시문을 고칠 때마다 그 문구가 사라져서, 멀쩡한 최신 지시문을 "옛것"이라고 보고했다(#151).
# 확인 수단이 거짓말을 하면 없느니만 못하다.
#
# 비교는 **md5 한 번**으로 한다(#152). 처음에는 앞뒤 200자를 이어 붙여 비교했는데,
# 구분자로 쓴 `\x01`이 psql에서는 리터럴 4글자, 파이썬에서는 1바이트가 되어
# **길이가 같으면 항상 "내용이 다르다"** 고 나왔다 — 거짓 음성을 고치면서 새 거짓 음성을 만든 것이다.
# postgres의 `md5()`는 기본 내장이고(확장 불필요) UTF8 DB에서는 파이썬의
# `md5(t.encode("utf-8"))`와 같은 값을 준다. 이스케이프가 전혀 필요 없다.
set -uo pipefail

COMPOSE_FILE="${COMPOSE_FILE:-docker-compose.dev.yml}"
PG_USER="${PG_USER:-agent}"

_here=$(cd "$(dirname "$0")" && pwd)
for _cand in "$_here/.." "$_here" "$PWD"; do
  if [ -f "$_cand/$COMPOSE_FILE" ]; then cd "$_cand" && break; fi
done
if [ ! -f "$COMPOSE_FILE" ]; then
  echo "[확인] $COMPOSE_FILE 을 찾지 못했습니다. 저장소 루트에서 실행하세요." >&2
  exit 1
fi

SRC="shared/agent_instruction.py"
[ -f "$SRC" ] || { echo "[확인] $SRC 가 없습니다(rsync 먼저)." >&2; exit 1; }

# 파일에서 지시문을 읽는다 - 콘솔의 되돌리기 버튼과 **같은 방식**이다(모듈 캐시를 타지 않는다, #147).
WANT=$(python3 - "$SRC" <<'PY'
import hashlib, sys
ns = {}
exec(compile(open(sys.argv[1], encoding="utf-8").read(), sys.argv[1], "exec"), ns)
t = ns["AGENT_INSTRUCTION"]
print(f"{len(t)} {hashlib.md5(t.encode('utf-8')).hexdigest()}")
PY
)
WANT_LEN=${WANT%% *}
WANT_MD5=${WANT##* }
if [ -z "${WANT_MD5:-}" ] || [ "$WANT_LEN" = "$WANT_MD5" ]; then
  echo "[확인] 코드에서 지시문을 읽지 못했습니다." >&2
  exit 1
fi

PG_CID=$(docker compose -f "$COMPOSE_FILE" ps -q postgres 2>/dev/null | head -1)
if [ -z "$PG_CID" ]; then
  echo "[확인] postgres 컨테이너를 찾지 못했습니다." >&2
  exit 1
fi

GOT=$(docker exec -i "$PG_CID" psql -U "$PG_USER" -d platform_config -tAc \
  "select length(value) || ' ' || md5(value) from platform_settings
    where key = 'agent_system_instruction'" 2>/dev/null | tr -d '\r')
GOT=$(printf '%s' "$GOT" | tr -s ' ' | sed 's/^ *//;s/ *$//')

if [ -z "$GOT" ]; then
  echo "[확인] DB에 agent_system_instruction 이 없습니다." >&2
  exit 1
fi
GOT_LEN=${GOT%% *}
GOT_MD5=${GOT##* }

echo "코드 : ${WANT_LEN}자  md5 ${WANT_MD5}"
echo "DB   : ${GOT_LEN}자  md5 ${GOT_MD5}"
echo

if [ "$GOT_MD5" = "$WANT_MD5" ]; then
  echo "✅ 최신입니다. DB의 지시문이 지금 코드와 같습니다."
  exit 0
fi

echo "❌ 다릅니다."
if [ "$GOT_LEN" = "$WANT_LEN" ]; then
  echo "   길이는 같은데 내용이 다릅니다 — 관리자가 콘솔에서 직접 수정한 문구가 있을 수 있습니다."
else
  echo "   길이가 $((GOT_LEN - WANT_LEN))자 차이납니다."
fi
echo
echo "   조치: 콘솔 설정 탭 → '지시문을 최신 기본값으로 되돌리기' → agent-server 재시작."
echo "   그래도 그대로면 admin-console 을 재시작하세요(모듈 캐시, #147)."
exit 1
