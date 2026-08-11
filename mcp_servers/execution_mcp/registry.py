"""
`execution_commands`(관리자 콘솔 실행 탭)의 행을 **MCP 툴 하나씩**으로 노출한다.

왜 검색(RAG)이 아니라 툴인가:
  예전에는 카탈로그를 하이브리드 검색해 LLM에게 후보를 넘겼다. 그런데 "내 홈 스토리지 용량"이
  `myquota`를 못 찾는 일이 반복됐다 - 검색이 한 번 어긋나면 등록해 둔 커맨드가 통째로 없는 것처럼
  취급된다. 툴로 노출하면 LLM이 **목록을 직접 보고** 고르므로 그런 실패가 없다(#105, #106).

인자 설계:
  콘솔에서 `head -n {lines} {path}`처럼 **자리표시자가 든 커맨드 한 줄**을 적고, 각 자리표시자의
  타입/필수/기본값을 정한다. 그러면 LLM에게는 `lines`, `path`가 **타입이 붙은 파라미터**로 보인다
  (예전 Command MCP의 `args` 리스트 하나보다 훨씬 정확하게 채운다).
  그 위에 자유 인자(`args`)를 덧붙이는 것은 **항상 허용**한다 - 어떤 인자가 필요한지는 질문마다
  달라서 관리자가 미리 정할 수 없고, 위험한 인자는 차단 목록이 어차피 막는다(#128).
  `{user_id}`는 예약어라 LLM에 노출되지 않고 호출자 신원에서 강제 주입된다.

트레이드오프(알고 택한 것):
  - 툴 목록이 등록 개수만큼 커진다. 설명이 전부 프롬프트에 실리므로 상한을 둔다
    (`execution_tools_max`). 넘치면 남는 것은 툴로 못 내보내고 경고를 남긴다(#108).
  - 등록 내용을 고치면 **Execution MCP 재시작**이 필요하다(툴 목록이 기동 시 1회 구성됨).
"""
import asyncio
import inspect
import json
import os
import re
import sys
from typing import Annotated, Literal

import asyncpg
from pydantic import Field

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from ssh_exec import run_ssh_as_user  # noqa: E402
from execution_exec import (  # noqa: E402
    DEFAULT_DENY_CSV, DEFAULT_USER_SCOPE_CSV, build_registered_argv, choice_value,
    deny_set, tool_name_for, user_flag_set,
)

# 실측(#108): 툴 하나가 매 요청 프롬프트에서 약 270자 ≈ 100토큰을 쓴다(스키마 고정분 + 설명).
# 지시문 ~4.9k + 내장 툴이 이미 고정으로 나가고 검색 결과·대화 이력에 15k 안팎이 필요하므로,
# 등록 커맨드 예산은 8k토큰(=80개) 정도가 상한이다.
DEFAULT_MAX_TOOLS = 80

# 툴 하나가 프롬프트에서 차지하는 JSON 스키마의 고정분(이름·파라미터 틀). 설명 길이와 무관하게
# 항상 따라붙는다 - 측정값 약 215자.
_TOOL_SCHEMA_OVERHEAD = 215

_PY_TYPES = {"str": str, "int": int, "enum": str}

# 인자 설명은 파라미터 하나당 이만큼까지만 스키마에 싣는다. 툴 설명(300자)과 마찬가지로
# **매 요청** 프롬프트에 통째로 실리기 때문이다. 옵션 목록을 붙이면 금방 길어진다.
MAX_ARG_DESC = 240


def _annotation(spec: dict):
    """인자 정의 한 개 -> LLM 스키마에 실릴 타입 어노테이션.

    예전에는 `option: str = ''`처럼 **타입만** 넘겼다. 그래서 관리자가 콘솔에 적어 둔
    "`-j`: JSON 형식으로 반환 / `-tl`: 부가 정보 출력" 같은 인자 설명이 모델에 **한 글자도
    전달되지 않았다** - 등록은 했는데 에이전트가 옵션을 못 채우는 원인이었다(#140).
    `Annotated[T, Field(description=...)]`로 주면 FastMCP가 JSON 스키마의 해당 파라미터
    설명으로 넣어 준다. enum이면 Literal로 줘서 스키마에 선택지가 박히게 한다.
    """
    kind = spec.get("type", "str")
    desc = (spec.get("description") or "").strip()
    values = [v for v in (choice_value(c) for c in (spec.get("choices") or [])) if v]

    if kind == "enum" and values:
        # 선택지에 "값: 설명" 꼴로 적었으면 설명 쪽은 파라미터 설명으로 접어 넣는다.
        labels = [c.strip() for c in (spec.get("choices") or []) if str(c).strip()]
        if any(":" in lb for lb in labels):
            desc = (desc + " " if desc else "") + " / ".join(labels)
        # 선택형인데 필수가 아니면 **빈 값**도 스키마에 넣어야 한다. 그러지 않으면 기본값
        # `""`이 Literal에 없어 pydantic이 스키마를 만들다 죽고, 툴 하나가 통째로 사라진다.
        if not spec.get("required") and "" not in values:
            values = values + [""]
        base = Literal[tuple(values)]
    else:
        base = _PY_TYPES.get(kind, str)

    if not desc:
        return base
    return Annotated[base, Field(description=desc[:MAX_ARG_DESC])]


def estimate_prompt_tokens(descriptions: list[str]) -> tuple[int, int]:
    """노출된 툴들이 매 요청 프롬프트에서 쓰는 (글자 수, 대략의 토큰 수).

    토큰 수는 추정이다 - 폐쇄망에 Qwen 토크나이저를 두는 대신 한글 1.2자/토큰,
    나머지(ASCII·JSON 기호) 3.5자/토큰으로 환산한다. 자릿수를 보려는 값이다.
    """
    chars = tokens = 0
    for d in descriptions:
        text = (d or "") + " " * _TOOL_SCHEMA_OVERHEAD
        kor = len(re.findall(r"[가-힣]", text))
        chars += len(text)
        tokens += round(kor / 1.2 + (len(text) - kor) / 3.5)
    return chars, tokens


def _describe(row: dict) -> str:
    """LLM에 보일 설명. **짧게** - 툴 하나당 설명이 통째로 매 요청에 실린다(#108).
    공통 규칙("본인 권한으로 실행된다" 등)은 지시문에 한 번만 두고 여기서는 뺀다."""
    parts = [(row.get("description") or "").strip()]
    parts.append(f"[{row['exec_command']}]")
    return " ".join(p for p in parts if p)[:300]


def build_entry(row: dict, login_host_getter) -> dict:
    """등록 커맨드 한 행 -> build_wrapped가 받는 형태의 항목."""
    exec_command = row["exec_command"]
    arg_specs = row.get("args") or []
    allow_extra = True      # 항상 허용(#128). 어떤 인자가 필요한지는 에이전트가 판단한다.

    async def handler(user_id: str, host: str = "", **kwargs) -> dict:
        extra = kwargs.pop("args", None)
        deny = deny_set(await _deny_csv())
        argv = build_registered_argv(exec_command, arg_specs, kwargs, extra,
                                     user_id, deny, allow_extra,
                                     user_flag_set(await _user_flags_csv()))
        target = (host or "").strip() or await login_host_getter()
        return await run_ssh_as_user(target, user_id, argv)

    # LLM에 보일 파라미터: 콘솔에서 정의한 인자들(+ allow_extra_args면 args).
    # user_id는 항상 감춰지고, host는 host_mode='login_server'면 build_wrapped가 감춘다.
    params = [
        inspect.Parameter("user_id", inspect.Parameter.POSITIONAL_OR_KEYWORD, annotation=str),
        inspect.Parameter("host", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                          default="", annotation=str),
    ]
    annotations = {"user_id": str, "host": str}
    for spec in arg_specs:
        ann = _annotation(spec)
        default = inspect.Parameter.empty if spec.get("required") else (spec.get("default") or "")
        params.append(inspect.Parameter(spec["name"], inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        default=default, annotation=ann))
        annotations[spec["name"]] = ann
    if allow_extra:
        params.append(inspect.Parameter("args", inspect.Parameter.POSITIONAL_OR_KEYWORD,
                                        default=None, annotation=list))
        annotations["args"] = list
    # 기본값 없는 파라미터가 뒤에 오면 시그니처가 만들어지지 않는다.
    params.sort(key=lambda p: p.default is not inspect.Parameter.empty)
    handler.__signature__ = inspect.Signature(params)
    handler.__annotations__ = annotations

    return {
        "handler": handler,
        "description": _describe(row),
        # 프롬프트 예산 계산용. 인자 설명도 스키마에 실리므로 툴 설명과 함께 세야 한다.
        "schema_text": _describe(row) + " " + " ".join(
            str(_annotation(s)) for s in arg_specs),
        "enabled": bool(row.get("enabled", True)),
        "required_roles": list(row.get("required_roles") or []),
        "user_scoped": True,
        "scope_param": "user_id",
        "host_mode": row.get("host_mode") or "login_server",
    }


_deny_csv_getter = None
_user_flags_getter = None


def set_deny_csv_getter(getter):
    """설정에서 차단 목록을 읽는 함수를 주입한다(이 모듈이 config_store에 묶이지 않게)."""
    global _deny_csv_getter
    _deny_csv_getter = getter


def set_user_flags_getter(getter):
    """설정에서 '다른 사용자 지정 옵션' 목록을 읽는 함수를 주입한다."""
    global _user_flags_getter
    _user_flags_getter = getter


async def _deny_csv() -> str:
    if _deny_csv_getter is None:
        return DEFAULT_DENY_CSV
    return await _deny_csv_getter()


async def _user_flags_csv() -> str:
    if _user_flags_getter is None:
        return DEFAULT_USER_SCOPE_CSV
    return await _user_flags_getter()


def load_registered_sync(login_host_getter, dsn_key: str = "execution_db_dsn") -> tuple[dict, int]:
    """(툴 dict, 상한 때문에 빠진 개수). 기동 시 1회 호출."""
    async def _run():
        config_dsn = os.environ.get("CONFIG_DB_DSN")
        if not config_dsn:
            return {}, 0
        conn = await asyncpg.connect(config_dsn)
        try:
            dsn = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = $1", dsn_key)
            raw_max = await conn.fetchval(
                "SELECT value FROM platform_settings WHERE key = 'execution_tools_max'")
        finally:
            await conn.close()
        if not dsn:
            return {}, 0
        try:
            max_tools = int(raw_max) if raw_max else DEFAULT_MAX_TOOLS
        except (TypeError, ValueError):
            max_tools = DEFAULT_MAX_TOOLS

        c2 = await asyncpg.connect(dsn)
        try:
            # **비활성 커맨드는 툴로 내보내지 않는다.** 실행 시점 검사만으로 막으면 툴 설명이
            # 매 요청 프롬프트에 계속 실리고(하나당 ~100토큰), 에이전트가 골라서 호출한 뒤
            # "비활성입니다" 오류를 받는 헛턴을 돈다. 끄는 즉시 막히는 건 그대로다
            # (_is_enabled가 호출 시점에 또 확인한다). 다시 켜면 재시작 후 목록에 나타난다.
            total = await c2.fetchval(
                "SELECT count(*) FROM execution_commands WHERE enabled")
            disabled = await c2.fetchval(
                "SELECT count(*) FROM execution_commands WHERE NOT enabled") or 0
            rows = await c2.fetch(
                "SELECT tool_name, title, description, exec_command, args, "
                "host_mode, enabled, required_roles FROM execution_commands "
                "WHERE enabled ORDER BY title LIMIT $1", max_tools)
        finally:
            await c2.close()

        tools, taken = {}, set()
        for r in rows:
            row = dict(r)
            row["args"] = json.loads(row["args"]) if isinstance(row["args"], str) else (row["args"] or [])
            name = row.get("tool_name") or tool_name_for(row["title"], taken, row["exec_command"])
            if name in taken:                      # DB에 중복이 들어와도 툴이 사라지지 않게
                name = tool_name_for(row["title"], taken, row["exec_command"])
            taken.add(name)
            tools[name] = build_entry(row, login_host_getter)
        if disabled:
            print(f"[execution-mcp] 비활성 커맨드 {disabled}개는 툴 목록에서 제외했습니다"
                  "(프롬프트 예산 절약). 다시 켜면 재시작 후 나타납니다.")
        return tools, max(0, (total or 0) - len(rows))

    try:
        return asyncio.run(_run())
    except Exception as e:  # noqa: BLE001
        print(f"[execution-mcp] 등록 커맨드 로드 실패, 내장 툴만으로 기동: {type(e).__name__}: {e}")
        return {}, 0
