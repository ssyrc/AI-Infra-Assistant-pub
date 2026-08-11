#!/usr/bin/env bash
# 커맨드 실행이 **어디서** 느린지 초 단위로 쪼개 본다. 폐쇄망 배포 호스트(10.0.0.30)에서 실행.
#
#   bash scripts/bench-exec.sh <계정> [커맨드] [호스트IP]
#   예) bash scripts/bench-exec.sh ops.user "phd list"
#
# 한 번의 커맨드 실행 시간은 세 덩어리다.
#   (1) 접속        TCP + 인증 협상. 다중화 마스터가 서 있으면 0에 가깝다.
#   (2) su - 로그인 셸  원격 계정의 프로필(/etc/profile, .bashrc, 모듈 초기화 …)을 매번 읽는다.
#                    다중화로 줄지 않고, 도구 호출 횟수만큼 그대로 곱해진다.
#   (3) 커맨드 자체
#
# 계측은 **컨테이너 안에서 한 번에** 돈다. 단계마다 `docker compose exec`를 새로 띄우면
# 그 기동 비용(약 1초)이 모든 숫자에 섞여 들어간다 — 첫 판에서 "접속만 1.11초"로 보였던 게 그거다.
set -u

USER_ID="${1:-}"
CMD="${2:-true}"
HOST="${3:-}"
SVC="${SVC:-execution-mcp}"
COMPOSE="docker compose -f docker-compose.dev.yml"

if [ -z "$USER_ID" ]; then
  echo "사용법: bash scripts/bench-exec.sh <계정> [커맨드] [호스트IP]" >&2
  exit 2
fi

if [ -z "$HOST" ]; then
  HOST="$($COMPOSE exec -T "$SVC" python -c \
    'import asyncio,sys; sys.path.insert(0,"/app/shared")
from config_store import get_config
print(asyncio.run(get_config("execution_host","10.0.0.100")))' 2>/dev/null \
    | tr -d '\r' | tr -d '[:space:]')"
  HOST="${HOST:-10.0.0.100}"
fi
echo "대상: root@$HOST · 실행 계정: $USER_ID · 커맨드: $CMD"
echo

# 컨테이너 안에서 전부 실행한다. 우리 코드와 **같은 옵션**을 써야 의미가 있다.
$COMPOSE exec -T "$SVC" sh -s <<EOF
set -u
HOST='$HOST'
USER_ID='$USER_ID'
CMD='$CMD'
SOCK=/tmp/.ssh-mux/bench
mkdir -p /tmp/.ssh-mux

BASE="-o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=accept-new \
-o UserKnownHostsFile=/root/.ssh/known_hosts_agent -o ConnectTimeout=8 \
-o GSSAPIAuthentication=no -o PreferredAuthentications=publickey -o AddressFamily=inet \
-i /root/.ssh/id_ed25519 -o IdentitiesOnly=yes"
MUX="-o ControlMaster=auto -o ControlPath=\$SOCK -o ControlPersist=300"

# 느린 핸드셰이크의 범인을 가리기 위해, 최적화를 **끈** 조건도 함께 잰다.
SLOW="-o BatchMode=yes -o PasswordAuthentication=no -o StrictHostKeyChecking=accept-new \
-o UserKnownHostsFile=/root/.ssh/known_hosts_agent -o ConnectTimeout=8 \
-i /root/.ssh/id_ed25519"

t() {   # t <라벨> <ssh옵션> <원격명령>
  label="\$1"; shift
  opts="\$1"; shift
  start=\$(date +%s%N)
  ssh \$opts "root@\$HOST" "\$1" >/dev/null 2>&1 </dev/null
  code=\$?
  end=\$(date +%s%N)
  # dash의 printf에는 부동소수 서식을 맡기지 않는다(밀리초를 정수로 쪼개 찍는다).
  ms=\$(( (end - start) / 1000000 ))
  printf '  %-42s %4d.%03d초  (종료코드 %d)\n' "\$label" \$(( ms / 1000 )) \$(( ms % 1000 )) "\$code"
}

ssh \$BASE \$MUX -O exit "root@\$HOST" >/dev/null 2>&1
rm -f "\$SOCK" 2>/dev/null

echo "=== 1. 첫 접속 (마스터 없음) — 이게 크면 인증 협상이 원인 ==="
t "최적화 끔 (기존 옵션)"        "\$SLOW" true
t "최적화 켬 (GSSAPI/키/IPv4)"   "\$BASE" true

echo
echo "=== 2. 마스터를 세운 뒤: 권한 강등 방식별 고정 비용 ==="
ssh \$BASE \$MUX "root@\$HOST" true >/dev/null 2>&1 </dev/null
t "접속만 (강등 없음)"                    "\$BASE \$MUX" true
t "su-login  : su - user -c true"         "\$BASE \$MUX" "su - \$USER_ID -c true"
t "su        : su user -c true"           "\$BASE \$MUX" "su \$USER_ID -c true"
t "runuser   : runuser -u user -- true"   "\$BASE \$MUX" "runuser -u \$USER_ID -- true"

echo
echo "=== 3. 실제 커맨드 ==="
t "su-login" "\$BASE \$MUX" "su - \$USER_ID -c '\$CMD'"
t "su"       "\$BASE \$MUX" "su \$USER_ID -c '\$CMD'"
t "runuser"  "\$BASE \$MUX" "runuser -u \$USER_ID -- \$CMD"

ssh \$BASE \$MUX -O exit "root@\$HOST" >/dev/null 2>&1
EOF

cat <<'EOF'

읽는 법
  · 1번에서 "최적화 켬"이 확 작아지면 → 인증 협상(GSSAPI/여러 키/IPv6)이 원인이었다.
    이번 코드에 그 옵션들이 들어갔으니 그대로 좋아진다.
  · 1번 두 줄이 둘 다 크면 → 게이트 서버 sshd 쪽 지연(역방향 DNS `UseDNS` 등)이다.
    우리 쪽에서 못 줄이니, 마스터를 항상 세워 두는 것(예열)으로 감춘다.
  · 2번에서 su-login 이 나머지 둘보다 크면 → 원격 계정 프로필이 무겁다. 그 차이가
    **커맨드 하나마다, 도구 호출 횟수만큼** 곱해진다.
  · **3번이 결정한다.** 셋 다 정상 출력이 나오면 가장 빠른 것을 쓰면 된다:
      .env 에  SSH_PRIVDROP=runuser   (또는 su / su-login)  → up -d 로 재생성
    su/runuser 에서만 `command not found` 가 나면 그 커맨드가 프로필의 PATH에 의존하는 것이니
    su-login 을 유지해야 한다. runuser 자체가 없으면 127로 실패한다(그때도 su-login/su를 쓴다).
  · 3번 셋 다 비슷하게 크면 → 커맨드 자체가 느린 것이다. 정상이다.
EOF
