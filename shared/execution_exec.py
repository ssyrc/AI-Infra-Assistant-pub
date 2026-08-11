"""
Execution MCP의 argv 조립 + 차단 목록(blacklist). 등록 커맨드와 미등록 커맨드가 **같은 문을** 쓴다.

커맨드는 두 갈래로 들어온다.
  (1) 등록 커맨드 - 관리자가 콘솔에 넣은 `exec_command` 템플릿. 예: `head -n {lines} {path}`
      `{이름}` 자리는 콘솔에서 정의한 인자(타입/필수/기본값)로 채워진다. 관리자가 승인한
      뼈대이므로 검사는 **덧붙인 인자만** 좁게 한다.
  (2) 미등록 커맨드 - 매뉴얼/VOC에서 찾았거나 LLM이 아는 커맨드를 그대로 실행(run_command).
      승인한 사람이 없으므로 **모든 토큰을 엄격하게** 훑는다.

차단이 필요한 이유(사용자 지적): `mpirun`, `docker run`, `xargs`처럼 **인자를 실행하는**
커맨드가 있다. 기본 명령만 보고 통과시키면 `mpirun -n 4 rm -rf /`가 그대로 나간다.
그래서 차단 검사는 argv[0]이 아니라 **모든 토큰**을 본다.

셸은 어느 경로에서도 쓰지 않는다(argv 리스트 그대로 exec). 그래서 `;`나 백틱이 인자에 있어도
치환·분리가 일어나지 않는다 - 다만 `bash -c "..."`처럼 **자기가 셸을 여는 커맨드**는 그 방어를
무력화하므로 셸 자체를 차단 목록에 넣는다.
"""
import hashlib
import os
import re
import shlex

# 명백히 파괴적이거나 권한을 넘기는 기본 명령. 완전한 안전장치가 아니라 최소한의 안전망이다.
# 관리자 콘솔 설정 `execution_deny_commands`로 조정할 수 있고, 비우면 제한이 사라진다.
DENY_BASE_COMMANDS = {
    # 파괴
    "rm", "dd", "mkfs", "shred", "wipefs", "fdisk", "parted", "mkswap", "truncate",
    # 전원/서비스
    "shutdown", "reboot", "poweroff", "halt", "init", "systemctl", "service",
    # 프로세스 종료
    "kill", "killall", "pkill",
    # 계정/권한
    "useradd", "userdel", "usermod", "groupadd", "groupdel", "passwd", "visudo",
    "chmod", "chown", "chattr", "setfacl", "su", "sudo", "doas", "runuser", "setpriv",
    # 시스템 상태 변경
    "mount", "umount", "iptables", "nft", "crontab", "at",
    # **셸/실행 위임** - 이게 열려 있으면 위의 모든 차단이 무의미해진다.
    #   `bash -c "rm -rf /"`, `ssh host rm -rf /`, `docker run -v /:/host ... rm -rf /host`
    "sh", "bash", "zsh", "ksh", "csh", "tcsh", "dash", "eval", "exec", "source",
    "ssh", "scp", "sftp", "rsync", "docker", "podman", "kubectl", "nsenter", "chroot",
}

DEFAULT_DENY_CSV = ",".join(sorted(DENY_BASE_COMMANDS))

# 셸을 쓰지 않으므로 파이프/리다이렉션은 동작하지 않는다. 조용히 이상하게 실행되는 대신
# 무엇이 문제인지 알려주기 위해 명시적으로 거부한다.
_SHELL_OPERATORS = {"|", "||", "&", "&&", ";", ">", ">>", "<", "<<", "`", "$("}

# 엄격 검사에서 토큰을 다시 쪼갤 구분자. `bash -c "rm -rf /"`처럼 **한 토큰 안에 여러 낱말이
# 들어있는** 경우를 잡기 위한 것이다.
_WORD_SPLIT = re.compile(r"[\s;|&()`$<>\"']+")

MAX_ARGS = 32
MAX_ARG_LEN = 512

# ADK는 에이전트 이름을 시스템 프롬프트에 넣는다. 모델이 그걸 **사용자 계정으로 착각해서**
# `/home/ops_assistant` 같은 경로를 만들어 실행한 사고가 두 번 있었다(#125, #131).
# 지시문으로 두 번 막았는데도 재발했으므로, 실행 직전에 거부하고 무엇이 틀렸는지 알려 준다.
# 실제 계정은 호출자 헤더에서 주입되므로, 이 이름이 커맨드에 들어갈 이유가 전혀 없다.
AGENT_SELF_NAMES = {
    n.strip().lower()
    for n in os.environ.get("AGENT_SELF_NAMES", "ops_assistant,ai_infra_assistant").split(",")
    if n.strip()
}
PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

# **다른 사용자 계정을 가리키는 옵션.** 여기까지 막아야 "남의 자원 접근 불가"가 실제로 성립한다.
#
# 지금까지의 방어는 *실행 신원*만 고정했다: `X-User-Id`는 LLM 스키마에서 숨겨져 있고,
# `ssh root@host` → `runuser -u <호출자>`로 항상 본인 계정으로 강등해 실행한다. 그래서 파일
# 권한처럼 **OS가 판정하는 것**은 안전하다. 그런데 스케줄러 CLI는 다르다 -
# `phd list -u other.user`은 본인 계정으로 실행해도 **남의 job을 보여준다**. 판정 주체가
# OS가 아니라 그 프로그램이기 때문이다.
#
# 실제로 "other.user 계정이 어떤 gpu job 을 수행중이야?"에 에이전트가 거절하기는 했지만,
# 그건 지시문을 따른 **모델의 선택**이었을 뿐 강제가 아니었다(#140). 모델이 마음을 바꾸거나
# 등록 커맨드의 `{option}` 자리에 `-u 남의계정`을 채우면 그대로 나간다. 그래서 실행 직전에 끊는다.
#
# 설정 `execution_user_scope_flags`로 조정한다(비우면 검사 안 함). `sort -u`처럼 계정과 무관한
# `-u`를 쓰는 커맨드가 걸리면 목록에서 `-u`를 빼거나 그 커맨드를 등록해서 쓴다.
USER_SCOPE_FLAGS = {"-u", "--user", "--username", "--owner", "--uid", "--as-user", "-U"}

DEFAULT_USER_SCOPE_CSV = ",".join(sorted(USER_SCOPE_FLAGS))

ARG_TYPES = ("str", "int", "enum")
ARG_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
RESERVED_ARGS = {"user_id", "host"}
HOST_MODES = ("login_server", "target_server")


# MCP 툴 이름에 쓸 수 없는 문자. 등록 이름에는 공백·점·한글이 들어올 수 있다.
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_]")


def _stable_hash(text: str, length: int) -> str:
    """프로세스가 바뀌어도 같은 값이 나오는 짧은 해시(툴 이름 고정용).
    파이썬 hash()는 PYTHONHASHSEED로 매번 달라져 재시작할 때마다 툴 이름이 바뀐다."""
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:length]


def tool_name_for(title: str, taken: set[str], exec_command: str = "") -> str:
    """사람이 읽는 이름 -> 충돌 없는 ASCII 툴 이름.

    OpenAI 호환 함수 이름 규칙은 `[a-zA-Z0-9_-]{1,64}`라 한글 이름은 그대로 못 쓴다.
    한글이면 남는 글자가 없어 전부 같은 이름으로 뭉개지므로 **실행 커맨드에서 이름을 만들고**
    (`quota report -u {user_id}` -> `quota_report`), 그것도 없으면 고정 해시를 붙인다.
    의미는 툴 '설명'이 담으므로 이름은 서로 구분만 되면 된다.

    **shared에 두는 이유**: 이 규칙을 Execution MCP·관리자 콘솔·db-init(이관)이 모두 써야 한다.
    mcp_servers는 db-init/admin-console 컨테이너에 마운트되지 않아, MCP 쪽에 두면
    이관 단계가 조용히 건너뛰어진다(실제로 그렇게 실패했다).
    """
    def _ascii(text: str) -> str:
        cleaned = re.sub(r"\{[^}]*\}", " ", text or "")
        toks = [t for t in _UNSAFE_NAME.sub(" ", cleaned).split() if t and not t.isdigit()]
        return "_".join(toks[:2]).lower()

    base = _ascii(title) or _ascii(exec_command) or ("k" + _stable_hash(title, 6))
    if not base[0].isalpha():
        base = f"c_{base}"
    name = base[:60]
    if name not in taken:
        return name
    for i in range(2, 100):
        alt = f"{name[:57]}_{i}"
        if alt not in taken:
            return alt
    return f"{name[:52]}_{_stable_hash(title, 4)}"


def deny_set(csv_value: str | None) -> set[str]:
    """설정값(콤마 구분)을 차단 집합으로. 빈 값이면 제한 없음."""
    if csv_value is None:
        csv_value = DEFAULT_DENY_CSV
    return {t.strip().lower() for t in csv_value.split(",") if t.strip()}


def choice_value(choice) -> str:
    """선택지 한 칸에서 **실제 값**만 뽑는다. `"-j: JSON 형식으로 반환"` -> `"-j"`.

    관리자가 선택지마다 설명을 달 수 있게 하기 위한 것이다. 예전에는 선택지에 값만 쓸 수 있어서,
    `phd info {option}`처럼 옵션마다 뜻이 다른 커맨드를 등록할 때 설명을 전부 인자 설명 한 칸에
    몰아 써야 했다(사용자 지적, #140).

    "값: 설명"으로 보는 조건은 **콜론 뒤에 공백이 있고 앞이 공백 없는 한 낱말**일 때다.
    콜론만으로 자르면 `12:00`이나 `http://…` 같은 정상적인 값이 반토막 난다.
    """
    text = str(choice if choice is not None else "").strip()
    m = re.match(r"^(\S+):\s+\S", text)
    return m.group(1) if m else text


def _basename(word: str) -> str:
    """`/bin/rm` -> `rm`. 경로로 우회하는 것을 막는다."""
    return word.strip().lower().rsplit("/", 1)[-1]


def scan_denied(tokens: list[str], deny: set[str], *, strict: bool) -> str | None:
    """차단 대상이 있으면 그 이름을, 없으면 None을 돌려준다.

    strict=False (등록 커맨드에 덧붙인 인자):
      '맨 이름'처럼 보이는 토큰만 본다. 경로(`/data/kill`)나 옵션(`--rm`)은 건드리지 않는다 -
      관리자가 승인한 커맨드의 정상적인 인자를 막으면 안 된다.
    strict=True (미등록 커맨드):
      토큰을 낱말로 쪼개고 각 낱말의 맨 이름까지 본다. `bash -c "rm -rf /"`처럼 한 토큰 안에
      숨은 것도 잡는다. 경로에 우연히 들어간 이름도 걸릴 수 있는데, 그 경우는 커맨드를
      정식으로 등록해서 쓰면 된다(오탐보다 미탐이 위험하다).
    """
    if not deny:
        return None
    for token in tokens:
        text = str(token)
        if strict:
            for word in _WORD_SPLIT.split(text):
                if word and _basename(word) in deny:
                    return _basename(word)
        else:
            t = text.strip().lower()
            if t and "/" not in t and not t.startswith("-") and t in deny:
                return t
    return None


def user_flag_set(csv_value: str | None) -> set[str]:
    """설정값(콤마 구분)을 계정 지정 옵션 집합으로. 빈 값이면 검사하지 않음."""
    if csv_value is None:
        csv_value = DEFAULT_USER_SCOPE_CSV
    return {t.strip().lower() for t in csv_value.split(",") if t.strip()}


def _words(argv: list[str]) -> list[str]:
    """토큰 안에 공백으로 붙어 있는 낱말까지 펼친다.

    등록 커맨드는 `{option}` 한 자리에 `"-u other.user"`이 통째로 들어올 수 있다.
    `render_argv`가 토큰 경계를 치환 **전에** 정하므로 argv 상으로는 토큰 하나지만,
    원격 셸 없이 exec될 때 프로그램이 받는 것도 토큰 하나다... 가 아니라, `su -c`로 넘길 때
    한 낱말씩 다시 쪼개진다. 어느 쪽이든 **검사에서는 펼쳐서 본다**(미탐이 오탐보다 위험).
    """
    out = []
    for token in argv:
        out.extend(str(token).split())
    return out


def reject_other_user(argv: list[str], user_id: str, flags: set[str] | None = None) -> None:
    """`-u 남의계정`처럼 **다른 사용자를 지목하는 인자**를 실행 전에 거부한다.

    허용하는 값은 호출자 본인 계정뿐이다(`-u ops.user`는 통과 - 대부분의 CLI에서 본인 조회는
    정상적인 사용법이다). 빈 값도 통과시킨다(자리표시자가 비어 렌더된 경우).

    오류 문구가 **에이전트에게 그다음 행동까지 지시한다**: 실제 사고에서 모델이 거절은 했지만
    이어서 매뉴얼을 뒤져 "가이드 위치: 슈퍼컴 Portal > ..." 같은 안내를 붙였다. 물어본 것은
    권한이 없다는 사실 하나인데 문서 안내가 따라붙으면 답이 길어지기만 한다(#140).
    """
    flags = USER_SCOPE_FLAGS if flags is None else flags
    if not flags:
        return
    me = (user_id or "").strip().lower()
    words = _words(argv)

    def _violate(flag: str, value: str) -> None:
        raise PermissionError(
            f"'{flag} {value}'는 다른 사용자의 자원을 조회하려는 것이라 실행하지 않았습니다. "
            f"이 시스템은 본인({user_id}) 자원만 조회할 수 있습니다. "
            "**이 사실만 한 줄로 답하세요.** 매뉴얼이나 과거 사례를 찾지 말고, "
            "가이드 문서 위치를 안내하지 말고, 다른 방법을 제안하지 마세요.")

    for i, word in enumerate(words):
        low = word.lower()
        # `--user=남` / `-u=남`
        head, sep, tail = low.partition("=")
        if sep and head in flags:
            if tail and tail != me:
                _violate(head, word.split("=", 1)[1])
            continue
        # `-u 남` (다음 낱말이 값)
        if low in flags:
            nxt = words[i + 1] if i + 1 < len(words) else ""
            if nxt and not nxt.startswith("-") and nxt.lower() != me:
                _violate(low, nxt)
            continue
        # `-u남` (짧은 옵션에 값이 붙은 꼴). 이걸 빼면 위 두 검사를 그대로 우회할 수 있다.
        if len(low) > 2 and low[0] == "-" and low[1] != "-" and f"-{low[1]}" in flags:
            value = word[2:]
            if value.lower() != me:
                _violate(f"-{low[1]}", value)


def reject_self_name(argv: list[str], user_id: str) -> None:
    """에이전트가 **자기 이름**을 계정/경로로 써서 만든 커맨드를 거부한다.

    실제로 이런 일이 있었다: "내 홈 파일 리스트"에 `ls -la /home/ops_assistant`를 실행하고
    "경로가 존재하지 않습니다"라고 답했다. `ops_assistant`는 ADK가 시스템 프롬프트에 넣는
    **에이전트 자신의 이름**이지 사용자 계정이 아니다(#125에서 지어낸 `ls` 출력의 소유자로
    쓴 것과 같은 뿌리다). 지시문으로 두 번 막았는데 재발했으므로 실행 단계에서 끊는다.

    거부 메시지가 **다음에 무엇을 해야 하는지** 알려 주도록 썼다 — 실행은 언제나 본인 홈에서
    시작하므로 경로를 비우면 된다.
    """
    if not AGENT_SELF_NAMES:
        return
    for token in argv:
        low = str(token).lower()
        for name in AGENT_SELF_NAMES:
            if name and name in low and name != (user_id or "").lower():
                raise PermissionError(
                    f"'{token}'에 에이전트 자신의 이름이 들어 있어 실행하지 않았습니다. "
                    f"그건 사용자 계정이 아닙니다(질문한 사용자는 '{user_id}'이고, 계정은 "
                    "시스템이 자동으로 넣습니다). 홈 디렉토리를 보려면 **경로를 비우거나 '.'** 을 "
                    "쓰세요 — 실행은 항상 본인 홈에서 시작합니다.")


def check_token(token: str, where: str) -> str:
    if not isinstance(token, str):
        raise ValueError(f"{where}는 문자열이어야 합니다: {token!r}")
    if "\x00" in token or any(ord(c) < 32 for c in token):
        raise ValueError(f"{where}에 제어문자가 들어갈 수 없습니다.")
    if len(token) > MAX_ARG_LEN:
        raise ValueError(f"{where}가 너무 깁니다(최대 {MAX_ARG_LEN}자).")
    if token.strip() in _SHELL_OPERATORS:
        raise ValueError(
            f"셸 연산자({token.strip()})는 지원하지 않습니다. "
            "파이프/리다이렉션 없이 실행 가능한 단일 커맨드로 등록해 주세요.")
    return token


def split_command(text: str, where: str = "실행 커맨드") -> list[str]:
    try:
        argv = shlex.split(text or "")
    except ValueError as e:
        raise ValueError(f"{where}를 해석할 수 없습니다({text!r}): {e}")
    if not argv:
        raise ValueError(f"{where}가 비어 있습니다.")
    return [check_token(t, where) for t in argv]


def placeholders_in(exec_command: str) -> list[str]:
    """템플릿에 쓰인 `{이름}`을 등장 순서대로(중복 제거) 돌려준다. 콘솔 UI가 인자 표를 만들 때 쓴다."""
    seen, out = set(), []
    for name in PLACEHOLDER_RE.findall(exec_command or ""):
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


def validate_definition(tool_name: str, exec_command: str, args: list, host_mode: str,
                        deny: set[str], existing_names: set[str] = frozenset()) -> None:
    """콘솔에서 등록/수정할 때의 검증. 문제가 있으면 ValueError."""
    if not TOOL_NAME_RE.match(tool_name or ""):
        raise ValueError("툴 이름은 영문 소문자로 시작하고 소문자/숫자/밑줄만 쓸 수 있습니다(예: my_quota).")
    if tool_name in existing_names:
        raise ValueError(f"이미 있는 툴 이름입니다: {tool_name}")
    if host_mode not in HOST_MODES:
        raise ValueError(f"실행 위치는 {' 또는 '.join(HOST_MODES)} 중 하나여야 합니다.")

    argv = split_command(exec_command)
    hit = scan_denied(argv, deny, strict=True)
    if hit:
        raise ValueError(f"'{hit}'는 파괴적이거나 다른 명령을 대신 실행할 수 있어 등록할 수 없습니다.")

    seen = set()
    for a in args or []:
        name = (a.get("name") or "").strip()
        if not ARG_NAME_RE.match(name):
            raise ValueError(f"인자 이름이 올바르지 않습니다: {name!r} (영문 소문자로 시작)")
        if name in RESERVED_ARGS:
            raise ValueError(f"'{name}'는 예약된 이름입니다(시스템이 자동으로 넣습니다).")
        if name in seen:
            raise ValueError(f"인자 이름이 중복됩니다: {name}")
        seen.add(name)
        if a.get("type", "str") not in ARG_TYPES:
            raise ValueError(f"지원하지 않는 인자 타입입니다: {a.get('type')}")
        if a.get("type") == "enum" and not [c for c in (a.get("choices") or []) if str(c).strip()]:
            raise ValueError(f"'{name}'은 선택형(enum)인데 선택지가 비어 있습니다.")

    used = set(placeholders_in(exec_command)) - RESERVED_ARGS
    missing = used - seen
    if missing:
        raise ValueError(
            f"커맨드에 쓴 자리표시자가 인자 목록에 없습니다: {', '.join('{%s}' % m for m in sorted(missing))}")
    unused = seen - used
    if unused:
        raise ValueError(
            f"인자를 정의했는데 커맨드에서 쓰지 않았습니다: {', '.join(sorted(unused))}. "
            "커맨드에 {이름} 형태로 넣거나 인자를 지우세요.")


def cast_arg(spec: dict, raw) -> str:
    """콘솔에서 정의한 타입대로 값을 확인하고 문자열로 만든다."""
    name = spec.get("name")
    kind = spec.get("type", "str")
    if kind == "int":
        try:
            return str(int(str(raw).strip()))
        except (TypeError, ValueError):
            raise ValueError(f"'{name}'은 정수여야 합니다: {raw!r}")
    text = str(raw)
    if kind == "enum":
        # 선택지는 "값" 또는 "값: 설명" 두 꼴을 모두 받는다. 비교는 항상 **값**으로 한다.
        choices = [choice_value(c) for c in (spec.get("choices") or [])]
        if text not in choices:
            raise ValueError(f"'{name}'은 {', '.join(choices)} 중 하나여야 합니다: {raw!r}")
    return text


def render_argv(exec_command: str, values: dict) -> list[str]:
    """템플릿의 `{이름}`을 값으로 치환한다. **토큰 경계는 치환 전에 정해진다** -
    값에 공백이 있어도 토큰이 쪼개지지 않으므로 인자 하나가 여러 개로 늘어날 수 없다."""
    out = []
    for token in split_command(exec_command):
        rendered = PLACEHOLDER_RE.sub(
            lambda m: str(values.get(m.group(1), m.group(0))), token)
        out.append(check_token(rendered, "실행 커맨드"))
    return out


def normalize_extra(args) -> list[str]:
    """LLM이 리스트 대신 문자열 한 줄로 주는 경우("-l /home")를 흡수한다."""
    if isinstance(args, str):
        try:
            extra = shlex.split(args)
        except ValueError as e:
            raise ValueError(f"인자를 해석할 수 없습니다({args!r}): {e}")
    else:
        extra = list(args or [])
    if len(extra) > MAX_ARGS:
        raise ValueError(f"인자가 너무 많습니다(최대 {MAX_ARGS}개).")
    return [check_token(str(a), "인자") for a in extra]


def build_registered_argv(exec_command: str, arg_specs: list, values: dict,
                          extra, user_id: str, deny: set[str],
                          allow_extra_args: bool,
                          user_flags: set[str] | None = None) -> list[str]:
    """등록 커맨드의 argv를 만든다(관리자가 승인한 템플릿 + 정의된 인자 + 선택적 추가 인자)."""
    filled = {"user_id": user_id}
    for spec in arg_specs or []:
        name = spec["name"]
        if name in values and values[name] not in (None, ""):
            filled[name] = cast_arg(spec, values[name])
        elif spec.get("default") not in (None, ""):
            filled[name] = cast_arg(spec, spec["default"])
        elif spec.get("required"):
            raise ValueError(f"'{name}' 인자가 필요합니다.")
        else:
            filled[name] = ""
    argv = [t for t in render_argv(exec_command, filled) if t != ""]

    extra = normalize_extra(extra)
    if extra and not allow_extra_args:
        raise ValueError("이 커맨드는 정해진 인자 외에 추가 인자를 받지 않습니다.")
    if extra:
        hit = scan_denied(extra, deny, strict=False)
        if hit:
            raise PermissionError(
                f"인자로 준 '{hit}'는 파괴적이거나 다른 명령을 대신 실행할 수 있어 거부했습니다.")
        # `{user_id}`로 호출자를 고정한 커맨드에서 같은 옵션을 다시 주면 값이 덮인다
        # (`<커맨드> -u 나 -u 남` -> 대부분의 CLI가 뒤엣것을 쓴다). 남의 자원을 볼 수 있게 되므로
        # 이미 고정된 옵션의 재지정 자체를 막는다.
        if "{user_id}" in (exec_command or ""):
            fixed = {t for t in split_command(exec_command) if t.startswith("-") and len(t) > 1}
            for token in extra:
                if token.split("=", 1)[0] in fixed:
                    raise PermissionError(
                        f"'{token.split('=', 1)[0]}' 옵션은 호출자 계정으로 이미 고정돼 있어 "
                        "다시 지정할 수 없습니다(다른 사용자의 자원은 조회할 수 없습니다).")
    out = argv + extra
    reject_self_name(out, user_id)
    # 템플릿까지 포함해 검사한다. `{option}` 한 자리에 `-u 남의계정`이 통째로 들어오는 경로가
    # 있고(자유 인자만 보면 놓친다), 관리자가 실수로 남의 계정을 박아 등록한 경우도 걸러진다.
    reject_other_user(out, user_id, user_flags)
    return out


def build_free_argv(command: str, extra, user_id: str, deny: set[str],
                    user_flags: set[str] | None = None) -> list[str]:
    """미등록 커맨드(run_command)의 argv를 만든다. 승인한 사람이 없으므로 **엄격하게** 훑는다."""
    argv = split_command(command, "커맨드")
    argv = [user_id if t == "{user_id}" else t.replace("{user_id}", user_id) for t in argv]
    argv += normalize_extra(extra)

    hit = scan_denied(argv, deny, strict=True)
    if hit:
        raise PermissionError(
            f"'{hit}'는 파괴적이거나 다른 명령을 대신 실행할 수 있어 실행하지 않았습니다. "
            "실행이 필요한 커맨드라면 관리자 콘솔 실행 탭에 등록해 주세요"
            "(등록된 커맨드는 정해진 뼈대로만 실행됩니다).")
    reject_self_name(argv, user_id)
    reject_other_user(argv, user_id, user_flags)
    return argv
