"""
호출자(user_id) 권한으로 '원격 서버'에서 read-only 명령을 실행하는 유틸(System/Command MCP 공용).

토폴로지:
- 이 agent 호스트(예: 10.0.0.30)는 root로 뜬다. 대상 서버들에 root로 ssh 할 수 있다.
- 대상은 **IP로 직접 지정**하는 것을 원칙으로 한다(설정 `execution_host`).
  이름을 쓰면 이 호스트의 /etc/hosts에서 찾는다. 미등록 이름은 거부한다.
  이름 해석은 우리가 통제하지 못하는 파일에 의존해, 같은 이름이 다른 서버로 풀리면
  키가 등록되지 않은 곳에 붙어 전부 인증 실패한다(실제로 login-01이 그랬다).
- 실행: ssh root@<ip> 로 접속한 뒤, 원격에서 `su - <user_id> -c ...`로 '사용자 권한'으로 강등해
  명령을 실행한다. 남의 권한으로 실행할 수 없다.

보안:
- 로컬에서 셸을 쓰지 않는다(create_subprocess_exec, argv 리스트). 원격 명령 문자열은 shlex로
  이중 quote한다(root 셸 1회 + 사용자 셸 1회). host는 /etc/hosts 조회로만 얻고, user_id는
  엄격한 리눅스 계정명 정규식으로 검증하므로 메타문자가 들어갈 수 없다.
- BatchMode/PasswordAuthentication=no로 비밀번호 프롬프트에 걸려 멈추지 않는다.
- 타임아웃/출력 상한을 강제한다. rm 등 파괴적 명령은 상위(화이트리스트)에서 아예 노출하지 않는다.

환경변수(컨테이너에서 주입):
- HOSTS_FILE           대상 IP 매핑 파일 경로(기본 /etc/hosts)
- SSH_ROOT_USER        ssh 접속 계정(기본 root)
- SSH_KEY              ssh 개인키 경로(있으면 -i 로 사용)
- SSH_CONNECT_TIMEOUT  접속 타임아웃 초(기본 8)
"""
import os
import re
import time
import shlex
import asyncio
import hashlib
import ipaddress

HOSTS_FILE = os.environ.get("HOSTS_FILE", "/etc/hosts")
SSH_ROOT_USER = os.environ.get("SSH_ROOT_USER", "root")
SSH_KEY = os.environ.get("SSH_KEY", "")
# 컨테이너는 자주 재생성돼 known_hosts가 남지 않는다. 컨테이너 안 경로를 명시해 두면
# 매번 새로 만들어져도 문제가 없고, 호스트의 known_hosts와 충돌하지도 않는다.
SSH_KNOWN_HOSTS = os.environ.get("SSH_KNOWN_HOSTS", "/root/.ssh/known_hosts_agent")
# accept-new: 처음 보는 호스트는 자동 등록, 등록된 키와 '다르면' 거부(기본).
# 게이트 서버 키가 바뀌어 계속 막히면 .env에 SSH_STRICT_HOST_KEY=no 로 완화할 수 있다.
SSH_STRICT_HOST_KEY = os.environ.get("SSH_STRICT_HOST_KEY", "accept-new")
# `su - <user> -c ...`는 원격에 TTY가 없으면 PAM 설정에 따라 인증 단계에서 실패할 수 있다
# (`docker compose exec`로 손으로 돌리면 TTY가 붙어서 되는데 에이전트에서만 안 되는 원인).
# ssh -tt로 TTY를 강제해 손으로 돌릴 때와 같은 조건을 만든다. 문제가 있으면 .env에서 끈다.
# 기본 false: 컨테이너에서 TTY 없이(`exec -T`) 실행해도 `su - <user> -c ...`가 정상 동작함을
# 확인했다. -tt는 출력에 CR/제어문자를 섞으므로, 필요한 환경에서만 .env로 켠다.
SSH_FORCE_TTY = os.environ.get("SSH_FORCE_TTY", "false").strip().lower() == "true"
try:
    SSH_CONNECT_TIMEOUT = int(os.environ.get("SSH_CONNECT_TIMEOUT", "8"))
except ValueError:
    SSH_CONNECT_TIMEOUT = 8

# ssh 연결 다중화(ControlMaster). 커맨드 하나마다 TCP+SSH 핸드셰이크와 `su -` 로그인 셸을
# 새로 여는 게 지연의 대부분이다(요청당 1~3초). 첫 연결만 실제로 맺고 이후 커맨드는 그 위에
# 채널만 얹으면 두 번째부터는 왕복이 거의 사라진다.
# ControlPersist 동안 마스터 프로세스가 살아 있다가 알아서 종료된다.
SSH_CONTROL_DIR = os.environ.get("SSH_CONTROL_DIR", "/tmp/.ssh-mux")
SSH_MULTIPLEX = os.environ.get("SSH_MULTIPLEX", "true").strip().lower() != "false"
# 마스터가 살아 있는 시간. 300초(5분)면 잠깐 쉬었다 물어볼 때마다 재접속해 1~3초가 붙는다.
# 채팅이 드문드문 오는 사용 패턴이라 넉넉히 둔다(유휴 ssh 연결 하나의 비용은 무시할 만하다).
SSH_CONTROL_PERSIST = os.environ.get("SSH_CONTROL_PERSIST", "3600")
# 방화벽/NAT가 유휴 TCP를 조용히 끊으면 마스터 소켓만 남고 실제 연결은 죽는다. 그러면 다음
# 커맨드가 죽은 소켓을 잡고 타임아웃까지 기다린다. keepalive로 살아 있는지 스스로 확인하게 한다.
SSH_ALIVE_INTERVAL = os.environ.get("SSH_ALIVE_INTERVAL", "30")
SSH_ALIVE_COUNT = os.environ.get("SSH_ALIVE_COUNT", "3")
# 권한 강등 방식. **어느 쪽이든 호출자 본인 계정으로 내려가는 것은 같다**(우회 경로 없음).
# 차이는 커맨드 하나당 붙는 고정 비용이다.
#   su-login (기본) `su - <user> -c '...'` - 로그인 셸. 원격 계정 프로필(/etc/profile, .bashrc,
#                   모듈 초기화)을 매번 읽는다. 실측 약 2초. 사내 커맨드가 프로필의 PATH에
#                   의존하면 이것만 동작한다.
#   su              `su <user> -c '...'`   - 프로필을 안 읽는다. PATH가 기본값이 된다.
#   runuser         `runuser -u <user> -- <argv>` - PAM 인증 단계를 통째로 건너뛰고
#                   (root 전용 도구라 애초에 인증이 필요 없다) 프로필도 안 읽는다. 보통 제일 빠르다.
#                   **argv를 그대로 받아 셸 인용이 한 겹으로 줄어든다** - 안전 면에서도 낫다.
#                   util-linux에 들어 있어 RHEL/CentOS/Debian 계열엔 대개 있지만, 없으면 127로
#                   실패하므로 반드시 먼저 확인하고 바꾼다(scripts/bench-exec.sh가 셋 다 잰다).
_PRIVDROP_MODES = ("su-login", "su", "runuser")
SSH_PRIVDROP = os.environ.get("SSH_PRIVDROP", "").strip().lower()
if SSH_PRIVDROP not in _PRIVDROP_MODES:
    # 구 설정과의 호환: SSH_SU_LOGIN=false 는 `su`(비로그인)를 뜻했다.
    _legacy_login = os.environ.get("SSH_SU_LOGIN", "true").strip().lower() != "false"
    SSH_PRIVDROP = "su-login" if _legacy_login else "su"

# LLM에 넘길 출력 상한. **컨텍스트 예산 때문에 반드시 작아야 한다.**
# 예전엔 64KB였는데, 그 출력이 그대로 다음 요청 프롬프트에 실려 32768 컨텍스트를 넘겼다
# (실제로 nvidia-smi/job 목록 몇 번에 59,360 토큰이 됐다 - #123).
# 매뉴얼·VOC 검색 결과는 건당 1500자로 자르고 있었는데 커맨드 출력에만 상한이 없었다.
# 설정 `execution_result_max_chars`로 조정한다.
MAX_OUTPUT = 4000

_output_limit_getter = None


def set_output_limit_getter(getter):
    """출력 상한을 설정에서 읽는 함수를 주입한다(이 모듈이 config_store에 묶이지 않게)."""
    global _output_limit_getter
    _output_limit_getter = getter


async def _resolve_output_limit(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    if _output_limit_getter is None:
        return MAX_OUTPUT
    try:
        return int(await _output_limit_getter())
    except Exception:  # noqa: BLE001
        return MAX_OUTPUT
# 사내 커맨드는 느린 게 많다(GPFS 쿼터 조회처럼 스토리지 전체를 훑는 것들). 25초는 너무 짧아서
# 정상 동작하는 커맨드가 중단되고, 그러면 에이전트가 실패 원인을 엉뚱하게 해석한다.
# .env의 SSH_COMMAND_TIMEOUT으로 조정한다.
try:
    DEFAULT_TIMEOUT = int(os.environ.get("SSH_COMMAND_TIMEOUT", "120"))
except ValueError:
    DEFAULT_TIMEOUT = 120

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,253}$")
# 리눅스 계정명 형식. 사내 계정이 `ops.user`처럼 점을 포함하므로 '.'을 허용한다.
# 첫 글자를 [a-z_]로 고정해서 '-'로 시작하는 이름(ssh/su 옵션으로 해석될 수 있음)을 막는다.
_USER_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,63}$")

# 이 계정들로는 절대 실행하지 않는다(uid 0). agent 컨테이너가 root로 ssh하므로,
# user_id가 root로 들어오면 강등 없이 root로 실행되어 "절대 root 금지" 원칙이 깨진다.
DENY_USERS = {"root", "toor"}

# `su`가 계정을 못 찾았을 때의 대표적인 stderr 문구(배포판마다 조금씩 다름).
_NO_SUCH_USER = ("does not exist", "no passwd entry", "unknown id", "user not found")


def resolve_host(name: str) -> str:
    """접속할 대상의 IP를 돌려준다.

    **IP를 직접 주면 그 IP로 그대로 붙는다.** 이름은 HOSTS_FILE에서 찾는다.

    왜 IP를 우선하나: 배포 호스트의 /etc/hosts에서 `login-01`이 게이트 서버가 아니라
    전혀 다른 서버(10.0.1.7)로 풀리고 있었고, 그 서버에는 우리 키가 등록돼 있지 않아
    모든 커맨드 실행이 인증 실패했다. 이름 해석은 우리가 통제할 수 없는 파일에 의존하므로,
    로그인 서버는 설정에 **IP로** 박아 두고 이름 해석 자체를 타지 않게 한다.
    """
    target = (name or "").strip()
    try:
        # IPv4/IPv6 리터럴이면 이름 해석을 건너뛴다(/etc/hosts에 없어도 된다).
        ipaddress.ip_address(target)
        return target
    except ValueError:
        pass
    if not _HOSTNAME_RE.match(target):
        raise ValueError(f"잘못된 호스트명입니다: {name!r}")
    try:
        with open(HOSTS_FILE, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except OSError as e:
        raise RuntimeError(f"{HOSTS_FILE}를 읽을 수 없습니다: {e}")
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        ip, names = parts[0], parts[1:]
        if target == ip or target in names:
            return ip
    raise ValueError(
        f"{HOSTS_FILE}에 등록되지 않은 서버 이름입니다: {target}. "
        "이름 대신 IP로 지정하면 이름 해석을 타지 않습니다(예: 10.0.0.100).")


_control_dir_ready = False


def _ensure_control_dir() -> bool:
    """다중화 소켓을 둘 디렉토리를 준비한다(0700). 실패하면 다중화 없이 진행한다."""
    global _control_dir_ready
    if _control_dir_ready:
        return True
    try:
        os.makedirs(SSH_CONTROL_DIR, mode=0o700, exist_ok=True)
        os.chmod(SSH_CONTROL_DIR, 0o700)
        _control_dir_ready = True
    except OSError as e:
        print(f"[ssh_exec] ControlPath 디렉토리를 만들 수 없어 연결 다중화를 끕니다: {e}")
        _control_dir_ready = False
    return _control_dir_ready


def validate_user(user_id: str) -> str:
    if not user_id or not _USER_RE.match(user_id):
        raise PermissionError(
            f"리눅스 계정명 형식이 아니어서 실행할 수 없습니다: {user_id!r}. "
            "Open WebUI 계정 이메일의 '@' 앞부분이 서버 계정명과 같아야 합니다.")
    if user_id in DENY_USERS:
        raise PermissionError(
            f"'{user_id}' 계정으로는 실행할 수 없습니다. 커맨드는 반드시 일반 사용자 권한으로만 "
            "실행됩니다. 서버 계정과 연결된 일반 사용자 계정으로 로그인해 주세요.")
    return user_id


def _remote_command(user: str, argv: list, mode: str | None = None) -> str:
    """원격에서 'su - user -c <inner>' 형태로 사용자 권한 실행 명령을 만든다.
    inner의 동적 인자는 사용자 셸용으로 quote하고, inner 전체는 root 셸용으로 다시 quote한다.

    방식은 `SSH_PRIVDROP`으로 고른다(위 상수 설명 참고). **어느 쪽이든 호출자 본인 계정으로
    내려가는 것은 같다.**

    `runuser`는 인용이 **한 겹**이다(argv를 그대로 받는다). `su -c`는 문자열 하나를 받으므로
    사용자 셸용으로 한 번, root 셸용으로 또 한 번 - 두 겹으로 감싼다.
    """
    mode = mode or SSH_PRIVDROP
    if mode == "runuser":
        parts = ["runuser", "-u", user, "--", *(str(a) for a in argv)]
        body = " ".join(shlex.quote(p) for p in parts)       # root 셸 파싱용 한 겹뿐
    else:
        inner = " ".join(shlex.quote(str(a)) for a in argv)  # 사용자 셸 파싱용
        dash = "- " if mode == "su-login" else ""
        body = f"su {dash}{user} -c {shlex.quote(inner)}"    # root 셸 (user는 정규식 검증됨)
    return _with_home_cwd(body, user, mode)


def _with_home_cwd(body: str, user: str, mode: str) -> str:
    """비로그인 강등 모드에서 **작업 디렉토리를 사용자 홈으로** 옮긴다 (#144).

    `ssh root@host <cmd>`는 root의 홈(`/root`)에서 시작한다. 그런데 `runuser -u`와 `su`(비로그인)는
    **작업 디렉토리를 바꾸지 않는다** - `su - user`(로그인 셸)만 홈으로 이동한다.
    그래서 `SSH_PRIVDROP=runuser`로 바꾼 뒤 `ls -lh`가 `/root`에서 돌아
    `ls: cannot open directory '.': Permission denied`가 났다.

    지시문이 "실행은 항상 본인 홈에서 시작합니다"라고 말하고 있었으므로, 그 약속을 코드가
    지키게 만든다(모델에게 경로를 조립하라고 시키는 것은 #125·#131의 사고로 되돌아가는 길이다).

    `~user`는 **root 셸의 틸드 확장**이다. 계정명은 `_USER_RE`로 검증돼 셸 메타문자가 없으므로
    따옴표 없이 써도 안전하다(따옴표를 붙이면 확장 자체가 되지 않는다).

    `&&`가 아니라 `;`인 이유: root가 홈에 들어가지 못하는 환경(GPFS root_squash 등)에서
    `&&`면 커맨드가 아예 실행되지 않는다. `;`면 그런 환경에서도 **예전과 똑같이** 동작하고,
    들어갈 수 있는 환경에서만 고쳐진다 - 회귀 없이 개선만 남는다.
    """
    if mode == "su-login":
        return body            # 로그인 셸이 이미 홈으로 이동시킨다
    return f"cd ~{user} 2>/dev/null; {body}"


def control_path(ip: str) -> str:
    """다중화 소켓 경로. **ssh의 `%C` 대신 우리가 직접 만든다.**

    `%C`는 ssh가 내부에서 해싱하는 값이라 파이썬에서 같은 경로를 계산할 수 없다. 그러면
    "지금 마스터가 서 있나"를 밖에서 확인할 방법이 없어서, 느릴 때마다 접속 비용 때문인지
    커맨드 자체가 느린 것인지 추측하게 된다(#120에서 겪은 그대로다).
    경로를 우리가 정하면 `os.path.exists()` 한 번으로 재사용 여부를 알 수 있고,
    실행 결과에 그대로 실어 보낼 수 있다. 길이도 짧아 유닉스 소켓 제한(보통 108바이트)에 안 걸린다.
    """
    key = hashlib.sha1(f"{SSH_ROOT_USER}@{ip}".encode("utf-8")).hexdigest()[:16]
    return os.path.join(SSH_CONTROL_DIR, f"cm-{key}")


def master_socket_exists(ip: str) -> bool:
    """마스터 소켓 파일이 있는지(= 접속 비용 없이 채널만 얹을 수 있는 상태인지).
    `ssh -O check`(master_alive)보다 훨씬 싸서 매 커맨드마다 불러도 된다."""
    if not SSH_MULTIPLEX:
        return False
    try:
        return os.path.exists(control_path(ip))
    except OSError:
        return False


def _base_ssh_opts(ip: str = "", master: bool = False) -> list[str]:
    """모든 ssh 호출이 공유하는 옵션(다중화 포함). ip를 주면 그 호스트 전용 소켓을 쓴다.

    master=True는 **상주 마스터 프로세스**용이다(`ControlMaster=yes`, `ControlPersist=no`).
    ControlPersist를 주면 ssh가 스스로 백그라운드로 넘어가 버려서 우리 자식 프로세스가
    바로 끝나 버린다 - 그러면 살아 있는지 감시할 수가 없다. 전경에 붙들어 둬야 감시가 된다.
    ControlPath는 두 경우가 **반드시 같아야** 한다(다르면 재사용이 안 된다).
    """
    opts = [
        "-o", "BatchMode=yes",
        "-o", "PasswordAuthentication=no",
        "-o", f"StrictHostKeyChecking={SSH_STRICT_HOST_KEY}",
        "-o", f"UserKnownHostsFile={SSH_KNOWN_HOSTS}",
        "-o", f"ConnectTimeout={SSH_CONNECT_TIMEOUT}",
        "-o", f"ServerAliveInterval={SSH_ALIVE_INTERVAL}",
        "-o", f"ServerAliveCountMax={SSH_ALIVE_COUNT}",
        # --- 첫 접속(핸드셰이크)을 늘리는 것들을 끈다 -----------------------------------
        # 실측: 마스터가 없는 상태의 `ssh … true`가 **17.4초**였다. TCP 연결 자체가 아니라
        # 인증 협상 단계에서 대부분이 나간다. ConnectTimeout은 TCP 연결에만 걸려서 이걸 못 막는다.
        #   GSSAPI(커버로스): KDC가 없거나 안 닿는 망에서 조회가 타임아웃까지 매달린다.
        #     사내 폐쇄망에서 흔한 수 초~수십 초짜리 지연이고, 우리는 공개키만 쓴다.
        #   여러 키 시도: 에이전트/기본 경로의 키를 차례로 던지면 왕복이 그만큼 늘고,
        #     MaxAuthTries를 넘기면 진짜 키를 써 보기도 전에 끊긴다.
        #   IPv6: AAAA를 먼저 시도하다 떨어지면 그만큼 늦는다. 대상은 IPv4로 고정돼 있다.
        "-o", "GSSAPIAuthentication=no",
        "-o", "PreferredAuthentications=publickey",
        "-o", "AddressFamily=inet",
    ]
    if SSH_MULTIPLEX and _ensure_control_dir():
        opts += [
            "-o", "ControlMaster=yes" if master else "ControlMaster=auto",
            "-o", f"ControlPath={control_path(ip)}",
            "-o", "ControlPersist=no" if master else f"ControlPersist={SSH_CONTROL_PERSIST}",
        ]
    if SSH_KEY and os.path.isfile(SSH_KEY):
        # IdentitiesOnly: 지정한 이 키 **하나만** 시도한다(다른 키를 던져 보는 왕복을 없앤다).
        opts += ["-i", SSH_KEY, "-o", "IdentitiesOnly=yes"]
    return opts


async def warm_master(host: str) -> bool:
    """대상 호스트로의 ssh 마스터 연결을 미리 열어 둔다(다중화 전제).

    커맨드 실행의 체감 지연은 대부분 '첫 접속'이다 - TCP + 키 교환 + 원격 셸 기동.
    사용자가 무언가를 물어보기 전에 마스터를 띄워 두면, 실제 커맨드는 이미 열려 있는
    연결에 채널만 얹으므로 곧바로 실행된다. 실패해도 조용히 넘긴다(다음 커맨드가
    평소대로 직접 접속하면 되고, 여기서 서비스를 막을 이유가 없다).
    """
    if not SSH_MULTIPLEX:
        return False
    try:
        ip = resolve_host(host)
    except Exception as e:  # noqa: BLE001
        print(f"[ssh_exec] 예열 대상 호스트를 해석하지 못했습니다({host}): {e}")
        return False
    argv = ["ssh", *_base_ssh_opts(ip), f"{SSH_ROOT_USER}@{ip}", "true"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        _, err = await asyncio.wait_for(proc.communicate(), timeout=SSH_CONNECT_TIMEOUT + 5)
    except (asyncio.TimeoutError, OSError) as e:
        print(f"[ssh_exec] 연결 예열 실패({host}): {type(e).__name__}: {e}")
        return False
    if proc.returncode != 0:
        print(f"[ssh_exec] 연결 예열 실패({host}, 코드 {proc.returncode}): "
              f"{err.decode('utf-8', 'replace').strip()[:200]}")
        return False
    return True


async def master_alive(host: str) -> bool:
    """다중화 마스터 연결이 실제로 살아 있는지 확인한다(`ssh -O check`).

    "커맨드가 왜 느리냐"를 판단하는 데 이게 필요하다. 마스터가 죽어 있으면 매 커맨드가
    TCP+키교환+로그인 셸을 새로 열어(1~3초) 체감이 확 달라진다. 추측 대신 확인한다.
    """
    if not SSH_MULTIPLEX:
        return False
    try:
        ip = resolve_host(host)
    except Exception:  # noqa: BLE001
        return False
    argv = ["ssh", *_base_ssh_opts(ip), "-O", "check", f"{SSH_ROOT_USER}@{ip}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except (asyncio.TimeoutError, OSError):
        return False
    return proc.returncode == 0


# --- 상주 마스터 -------------------------------------------------------------------
# "서비스가 뜨면 10.0.0.100에 root 세션을 계속 띄워 두고, agent가 바로 su를 날릴 수 있게"
# (사용자 요구). 예전 방식은 `ssh … true`로 연결만 만들어 두고 ControlPersist에 수명을 맡기는
# 것이었다 - 어떤 이유로든 마스터가 죽으면 다음 확인(180초)까지 구멍이 났고, 그 사이 커맨드는
# 매번 새로 접속했다(실측 17~25초).
# 이제는 **마스터 ssh를 우리 자식 프로세스로 붙들고**(`-M -N`, 원격 명령 없음) 감시한다.
# 죽으면 즉시 다시 띄운다. 커맨드 실행 쪽은 그대로 `ControlMaster=auto`라, 마스터가 없더라도
# 스스로 접속해서 동작은 한다(느려질 뿐 끊기지 않는다).
_master_procs: dict = {}


async def _drain_stderr(ip: str, proc):
    """마스터의 stderr를 계속 읽어 로그로 넘긴다.
    읽지 않고 두면 파이프가 차서 ssh가 멈춘다(조용히 죽는 것보다 나쁘다)."""
    try:
        while True:
            line = await proc.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", "replace").strip()
            if text:
                print(f"[ssh_exec] 마스터({ip}) stderr: {text[:200]}")
    except Exception:  # noqa: BLE001
        pass


async def ensure_master(host: str) -> dict:
    """상주 마스터가 살아 있게 만든다(이미 살아 있으면 아무것도 하지 않는다).

    돌려주는 dict는 그대로 `/warm` 응답과 로그에 쓴다 - 밖에서 상태를 볼 수 있어야
    "느리다"를 추측하지 않는다.
    """
    if not SSH_MULTIPLEX:
        return {"ok": False, "reason": "다중화 꺼짐(SSH_MULTIPLEX=false)"}
    try:
        ip = resolve_host(host)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "reason": f"호스트 해석 실패({host}): {e}"}

    proc = _master_procs.get(ip)
    if proc is not None and proc.returncode is None and master_socket_exists(ip):
        return {"ok": True, "host": host, "ip": ip, "already_up": True}
    if proc is not None and proc.returncode is not None:
        print(f"[ssh_exec] 마스터({ip})가 종료돼 있었습니다(코드 {proc.returncode}). 다시 띄웁니다.")
        _master_procs.pop(ip, None)

    # 우리 프로세스가 아니어도 살아 있는 마스터가 있으면 그대로 쓴다(예: 재시작 직전에
    # ControlPersist로 백그라운드에 남아 있던 것). 여기서 새로 띄우면 소켓이 겹쳐 실패한다.
    if await master_alive(host):
        return {"ok": True, "host": host, "ip": ip, "already_up": True, "adopted": True}
    # 죽은 소켓 파일이 남아 있으면 새 마스터가 'already exists'로 못 뜬다. 지운다.
    try:
        os.unlink(control_path(ip))
    except OSError:
        pass

    started = time.monotonic()
    argv = ["ssh", *_base_ssh_opts(ip, master=True), "-M", "-N", f"{SSH_ROOT_USER}@{ip}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    except OSError as e:
        return {"ok": False, "reason": f"마스터를 띄우지 못했습니다: {type(e).__name__}: {e}"}
    asyncio.create_task(_drain_stderr(ip, proc))

    # `-N`은 원격 명령이 없어 계속 살아 있는 게 정상이다. 소켓이 생길 때까지만 잠깐 기다린다.
    deadline = time.monotonic() + SSH_CONNECT_TIMEOUT + 20
    while time.monotonic() < deadline:
        if proc.returncode is not None:
            return {"ok": False,
                    "reason": f"마스터가 곧바로 종료됐습니다(코드 {proc.returncode}). "
                              "ssh 키·호스트 키·네트워크를 확인하세요."}
        if master_socket_exists(ip) and await master_alive(host):
            _master_procs[ip] = proc
            took = int((time.monotonic() - started) * 1000)
            print(f"[ssh_exec] 상주 마스터 준비 완료({ip} · {took:,}ms). "
                  "이후 커맨드는 이 연결에 채널만 얹습니다.")
            return {"ok": True, "host": host, "ip": ip, "already_up": False,
                    "duration_ms": took}
        await asyncio.sleep(0.3)

    try:
        proc.kill()
    except ProcessLookupError:
        pass
    return {"ok": False, "reason": "마스터 연결이 제한 시간 안에 서지 않았습니다."}


def start_master_supervisor(host_getter, interval: int = 15):
    """상주 마스터를 감시한다. 죽어 있으면 곧바로 다시 띄운다.

    간격이 짧은 이유: 살아 있으면 소켓 파일 확인 한 번으로 끝나 거의 공짜다. 반대로 죽었을 때의
    대가는 크다(다음 커맨드가 17~25초). 예전 240초/180초 주기는 그 구멍을 그대로 뒀다.
    연속 실패 시에는 간격을 늘려(최대 5분) 안 되는 대상을 계속 두드리지 않는다.
    """
    async def _loop():
        fails = 0
        while True:
            wait = interval
            try:
                host = await host_getter()
                if host:
                    ip = resolve_host(host)
                    # 살아 있으면 여기서 끝(파일 확인 한 번).
                    if not (_master_procs.get(ip) is not None
                            and _master_procs[ip].returncode is None
                            and master_socket_exists(ip)):
                        res = await ensure_master(host)
                        if res.get("ok"):
                            fails = 0
                        else:
                            fails += 1
                            wait = min(interval * 2 ** min(fails, 5), 300)
                            print(f"[ssh_exec] 상주 마스터 실패({fails}회): "
                                  f"{res.get('reason')} · {wait}초 뒤 재시도")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                fails += 1
                wait = min(interval * 2 ** min(fails, 5), 300)
                print(f"[ssh_exec] 마스터 감시 오류(계속 진행): {type(e).__name__}: {e}")
            await asyncio.sleep(wait)

    return asyncio.create_task(_loop())


async def stop_masters():
    """상주 마스터를 정리한다(서비스 종료 시)."""
    for ip, proc in list(_master_procs.items()):
        try:
            if proc.returncode is None:
                proc.terminate()
        except ProcessLookupError:
            pass
        _master_procs.pop(ip, None)


def start_master_keepalive(host_getter, interval: int = 240):
    """마스터 연결이 끊기지 않게 주기적으로 예열한다(ControlPersist보다 짧은 주기).

    host_getter는 매번 호출되는 async 함수다 - 관리자가 콘솔에서 로그인 서버를 바꾸면
    다음 주기부터 새 주소를 예열한다.
    """
    async def _loop():
        while True:
            try:
                host = await host_getter()
                if host:
                    ok = await warm_master(host)
                    if not ok:
                        print(f"[ssh_exec] 마스터 연결 유지 실패({host}). 다음 커맨드는 "
                              "새로 접속하므로 1~3초 더 걸립니다.")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                print(f"[ssh_exec] 연결 예열 루프 오류(무시하고 계속): {type(e).__name__}: {e}")
            await asyncio.sleep(interval)

    return asyncio.create_task(_loop())


# 로그인 셸이 있어야 찾아지는 커맨드를 **런타임에 배운다**(커맨드 이름 -> 프로필 필요).
# 비로그인 모드(su/runuser)로 돌렸는데 `command not found`가 나면, 그건 그 커맨드가
# 프로필에서 잡히는 PATH에 있다는 뜻이다. 그 한 건만 로그인 셸로 다시 돌리고 여기 기억해 둔다.
# 다음부터 그 커맨드는 처음부터 로그인 셸로 간다 - 두 번 돌지 않는다.
_NEEDS_LOGIN_SHELL: set = set()
# 대상 서버에 runuser 자체가 없으면 모드를 통째로 내린다(커맨드마다 127을 반복할 이유가 없다).
_privdrop_downgrade: str | None = None

_NOT_FOUND_MARKERS = ("command not found", "no such file or directory", "not found")


def _effective_privdrop(argv: list) -> str:
    """이번 실행에 쓸 강등 방식. 학습된 예외와 서버 미지원을 반영한다."""
    mode = _privdrop_downgrade or SSH_PRIVDROP
    if mode != "su-login" and str(argv[0]).rsplit("/", 1)[-1] in _NEEDS_LOGIN_SHELL:
        return "su-login"
    return mode


def _looks_like_missing_command(returncode: int, stderr_low: str, argv: list) -> bool:
    """`command not found`인지. 커맨드 이름이 함께 보이는지까지 확인해 오탐을 줄인다."""
    if returncode not in (126, 127):
        return False
    name = str(argv[0]).rsplit("/", 1)[-1].lower()
    return any(m in stderr_low for m in _NOT_FOUND_MARKERS) and name in stderr_low


async def run_ssh_as_user(host: str, user_id: str, argv: list,
                          timeout: int = DEFAULT_TIMEOUT, max_output: int | None = None) -> dict:
    """host(=/etc/hosts 등록)로 ssh(root) 후 user_id 권한으로 argv를 실행한다(셸 주입 불가).

    비로그인 강등(`su`/`runuser`)으로 돌다가 커맨드를 못 찾으면 **로그인 셸로 한 번 더** 돌린다.
    그래서 `SSH_PRIVDROP`을 바꿔도 "안 되면 어쩌지"를 걱정할 필요가 없다 - 프로필이 필요한
    커맨드만 2초를 내고, 나머지는 계속 빠르다.
    """
    global _privdrop_downgrade
    ip = resolve_host(host)
    user = validate_user(user_id)
    max_output = await _resolve_output_limit(max_output)
    mode = _effective_privdrop(argv)

    # SSH_KEY 경로가 '파일'일 때만 -i로 넘긴다(_base_ssh_opts에서 처리). compose가 없는
    # 경로를 bind mount하면 도커가 그 자리에 '빈 디렉토리'를 만들어 버리는데, 그걸 -i로 주면
    # ssh가 무조건 인증 실패한다(서버에서 직접 ssh하면 되는데 에이전트로만 안 되는 원인).
    if SSH_KEY and not os.path.isfile(SSH_KEY):
        print(f"[ssh_exec] SSH_KEY가 파일이 아니라 무시합니다: {SSH_KEY} "
              "(docker가 빈 디렉토리를 만든 상태일 수 있음 - .env의 SSH_KEY_PATH 확인)")

    async def _attempt(run_mode: str):
        """주어진 강등 방식으로 한 번 실행한다."""
        argv_ssh = ["ssh", *_base_ssh_opts(ip)]
        if SSH_FORCE_TTY:
            argv_ssh.append("-tt")
        argv_ssh += [f"{SSH_ROOT_USER}@{ip}", _remote_command(user, argv, run_mode)]
        try:
            p = await asyncio.create_subprocess_exec(
                *argv_ssh,
                stdin=asyncio.subprocess.DEVNULL,   # -tt로 pty를 붙여도 입력 대기에 걸리지 않게
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "ssh 클라이언트가 없습니다. MCP 컨테이너에 openssh-client가 필요합니다.")
        try:
            o, e = await asyncio.wait_for(p.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            raise TimeoutError(
                f"명령이 {timeout}초 안에 끝나지 않아 중단했습니다({host}). 원래 오래 걸리는 "
                "커맨드라면 .env의 SSH_COMMAND_TIMEOUT을 늘리세요(권한/인증 문제가 아닙니다).")
        return p, o, e

    # "느리다"를 추측하지 않기 위한 두 값이다.
    #   reused=True  -> 접속은 공짜였다. 느렸다면 원격 커맨드나 `su -` 로그인 셸이 느린 것.
    #   reused=False -> 이 호출이 TCP+키교환+로그인까지 새로 했다(1~3초). 예열이 안 된 것.
    reused = master_socket_exists(ip)
    started = time.monotonic()
    proc, out, err = await _attempt(mode)

    # --- 비로그인 강등이 실패했을 때의 자동 복구 --------------------------------------
    # 여기서 되돌려 주지 않으면 `SSH_PRIVDROP`을 바꾸는 것이 도박이 된다.
    # 두 가지가 겹칠 수 있어서 **루프**로 돈다(runuser가 없는 서버에서 프로필이 필요한 커맨드).
    #   runuser 없음  -> 모드를 통째로 su로 내린다(한 번만, 이후 모든 커맨드에 적용).
    #   커맨드 못 찾음 -> 그 커맨드만 로그인 셸로. 이름을 기억해 다음부터는 처음부터 그렇게 간다.
    name = str(argv[0]).rsplit("/", 1)[-1]
    for _ in range(2):
        if mode == "su-login" or proc.returncode not in (126, 127):
            break
        err_low = err.decode("utf-8", "replace").lower()
        if mode == "runuser" and "runuser" in err_low and any(
                m in err_low for m in _NOT_FOUND_MARKERS):
            _privdrop_downgrade = "su"
            next_mode = "su" if name not in _NEEDS_LOGIN_SHELL else "su-login"
            print("[ssh_exec] 대상 서버에 runuser가 없어 강등 방식을 su로 내립니다"
                  "(이번 실행부터 자동 적용, .env를 고칠 필요 없음).")
        elif _looks_like_missing_command(proc.returncode, err_low, argv):
            _NEEDS_LOGIN_SHELL.add(name)
            next_mode = "su-login"
            print(f"[ssh_exec] '{name}'은 로그인 셸이 필요합니다 → 이번 실행만 su -로 다시 "
                  "합니다(다음부터는 처음부터 로그인 셸로 갑니다).")
        else:
            break
        if next_mode == mode:
            break
        mode = next_mode
        proc, out, err = await _attempt(mode)

    elapsed_ms = int((time.monotonic() - started) * 1000)

    # 잘렸는지를 **구조화된 값으로도** 돌려준다. 예전에는 안내 문구를 stdout 끝에 붙이는 게
    # 전부여서, 모델이 그 줄을 빼먹으면 사용자는 목록이 전부인 줄 알았다("중간에 잘리는 것 같아").
    clip_info = {"truncated": False, "total_lines": 0, "shown_lines": 0}

    def _clip(b: bytes, track: bool = False) -> str:
        # -tt로 pty를 쓰면 줄바꿈이 CRLF로 오고 "Connection to ... closed." 안내가 붙는다.
        s = b.decode("utf-8", "replace").replace("\r\n", "\n")
        s = "\n".join(line for line in s.split("\n")
                       if not line.startswith("Connection to ") or not line.endswith("closed."))
        lines = s.split("\n")
        if track:
            clip_info["total_lines"] = clip_info["shown_lines"] = len(lines)
        if max_output <= 0 or len(s) <= max_output:
            return s
        # **줄 단위로** 자른다. 표 형태 출력을 줄 중간에서 끊으면 에이전트가 값을 잘못 읽는다.
        kept, used = [], 0
        for line in lines:
            if used + len(line) + 1 > max_output and kept:
                break
            kept.append(line)
            used += len(line) + 1
        if track:
            clip_info.update(truncated=True, shown_lines=len(kept))
        return "\n".join(kept) + (
            f"\n…(전체 {len(lines)}줄 중 {len(kept)}줄만 보입니다. 출력이 길어 잘랐습니다. "
            "**답변에 '일부만 표시됐다'고 반드시 밝히세요.** 전체가 필요하면 조건을 좁혀 "
            "다시 실행하세요 - 여기 보이는 것만으로 답하고 전부라고 말하지 마세요.)")

    result = {
        "host": host,
        "ip": ip,
        "as_user": user,
        "command": " ".join(str(a) for a in argv),
        "exit_code": proc.returncode,
        "stdout": _clip(out, track=True),
        "stderr": _clip(err),
        "duration_ms": elapsed_ms,
        "connection_reused": reused,
        "privdrop": mode,
        **clip_info,
    }
    # 커맨드 하나가 몇 초 걸렸는지, 접속을 새로 맺었는지, 어떤 강등 방식이었는지를 **항상** 남긴다.
    # 이게 없으면 "느리다"는 리포트가 올 때마다 다시 추측하게 된다.
    # 실행한 커맨드를 **통째로** 남긴다. `ls`만 찍으면 `-A`가 빠져 숨김 파일이 안 나온 건지
    # 출력이 잘린 건지 구분할 수 없다. 잘렸으면 몇 줄 중 몇 줄인지도 함께 남긴다.
    clipped = (f" · 출력 {clip_info['total_lines']}줄 중 {clip_info['shown_lines']}줄만"
               if clip_info["truncated"] else "")
    print(f"[ssh_exec] {result['command']} → {elapsed_ms:,}ms "
          f"({'연결 재사용' if reused else '새 접속'} · {mode} · {ip} · {user}{clipped})")
    # 실패 원인을 에이전트가 엉뚱하게 해석하지 않도록, 흔한 두 가지는 명시적으로 알려준다.
    # **어느 IP로 붙었는지를 반드시 함께 적는다** - 손으로 IP를 직접 넣으면 되는데 에이전트만
    # 실패하는 경우, 원인은 거의 항상 "이름이 /etc/hosts에서 다른 IP로 풀렸다"이기 때문이다.
    low = result["stderr"].lower()
    # ssh가 남긴 진짜 사유 한 줄(디버그 잡음은 빼고). 추측 대신 이걸 보여준다.
    reason = next((ln.strip() for ln in result["stderr"].split("\n")
                   if ln.strip() and not ln.startswith("debug")), "")
    where = f"'{host}'({ip})"
    if proc.returncode == 255 and "host key verification" in low:
        result["error"] = (
            f"{where}의 ssh 호스트 키 확인에 실패해 커맨드가 실행되지 않았습니다"
            f"(인증서/계정 문제가 아님). known_hosts({SSH_KNOWN_HOSTS})에 다른 키가 등록돼 "
            f"있거나 StrictHostKeyChecking={SSH_STRICT_HOST_KEY} 설정이 막고 있습니다. "
            "컨테이너에서 해당 호스트 키를 지우거나(.env에 SSH_STRICT_HOST_KEY=no) 재시도하세요.")
    elif proc.returncode == 255 and ("permission denied" in low or "publickey" in low):
        if not SSH_KEY:
            detail = "SSH_KEY가 설정되지 않았습니다."
        elif not os.path.isfile(SSH_KEY):
            detail = (f"SSH_KEY 경로가 파일이 아닙니다({SSH_KEY}). compose가 없는 경로를 마운트해 "
                      "빈 디렉토리가 생긴 상태일 수 있습니다 - .env의 SSH_KEY_PATH를 실제 개인키 "
                      "파일로 지정하세요.")
        else:
            # 키 파일은 멀쩡한데 거부당했다면, 키가 아니라 '접속한 서버'가 다를 가능성이 크다.
            detail = (f"키 파일({SSH_KEY})은 정상입니다. '{host}'가 {HOSTS_FILE}에서 {ip}로 "
                      f"풀렸는데, 이 키가 등록된 서버가 {ip}가 맞는지 확인하세요"
                      "(이름이 의도한 서버가 아닌 다른 IP로 풀리는 경우가 가장 흔합니다).")
        result["error"] = (f"{where}에 ssh 인증이 실패해 커맨드가 실행되지 않았습니다"
                           f"(사용자 권한 문제가 아님). {detail}"
                           + (f" ssh 메시지: {reason}" if reason else ""))
    elif proc.returncode == 255:
        # 위 두 갈래에 안 걸리는 접속 실패(네트워크 불가, 타임아웃 등)도 그냥 넘기지 않는다.
        result["error"] = (f"{where}에 ssh 접속 자체가 실패했습니다."
                           + (f" ssh 메시지: {reason}" if reason else ""))
    elif proc.returncode != 0 and any(m in low for m in _NO_SUCH_USER):
        result["error"] = (
            f"서버 '{host}'에 '{user}' 계정이 없어 실행하지 못했습니다(권한 문제가 아님). "
            "Open WebUI 계정 이메일의 '@' 앞부분이 서버 계정명과 같아야 합니다.")
    elif mode == "runuser" and proc.returncode == 127 and "runuser" in low:
        # 위 자동 복구로도 안 됐다는 뜻이다(재시도까지 runuser로 돌았을 리는 없지만 방어).
        result["error"] = (
            f"서버 '{host}'에 runuser가 없어 실행하지 못했습니다(커맨드 문제가 아님). "
            ".env의 SSH_PRIVDROP을 su 또는 su-login으로 되돌리세요.")
    return result
