"""
Execution MCP - 커맨드 실행을 담당하는 **단 하나의** MCP. (구 Command MCP + System MCP)

둘로 나눠 뒀던 이유가 없어졌다. 실행 경로가 애초에 같았고(ssh_exec.run_ssh_as_user), 등록은
어느 쪽이든 결국 화이트리스트이며, 인자를 에이전트가 자유롭게 정할 수 있어야 하는 것도 같다.
탭이 둘이라 "이건 어디에 등록하지?"가 매번 생겼고, 재시작 규칙도 감사 로그도 두 벌이었다.

노출하는 툴은 **두 갈래뿐**이다(#128에서 코드 내장 커맨드 7개를 없앴다).
  1) **등록 커맨드**(execution_commands) - 콘솔에서 `head -n {lines} {path}`처럼 적어 등록한 것.
     자리표시자가 타입 붙은 파라미터로 LLM에 노출된다. 전부 콘솔에서 편집·삭제·on/off 된다.
  2) **run_command** - 등록되지 않은 커맨드(매뉴얼/VOC에서 찾았거나 LLM이 아는 리눅스 명령)를
     그대로 실행. 승인한 사람이 없으므로 차단 목록을 **엄격하게** 적용한다(shared/execution_exec).

공통 보장(어느 갈래든 동일):
- 셸을 쓰지 않는다(argv 리스트). 항상 `ssh root@host` 후 `su - <user_id>`로 **호출자 본인**
  권한으로 강등해 실행한다. root로 실행되는 경로가 없다.
- user_id는 LLM 스키마에서 감추고 호출자 헤더에서 강제 주입한다(남의 자원 접근 불가).
- 전건 감사 로그(job_logs).

속도(#128, #132): 툴 호출 하나가 사용자를 기다리게 하는 시간은 (a) 상태 조회 DB 왕복,
(b) 감사 로그 INSERT, (c) ssh 접속, (d) 원격 커맨드다. (a)는 한 번으로 합쳤고, (b)는 성공
경로에서 응답 뒤로 미뤘다. (c)는 **서비스가 뜰 때 로그인 서버로 root ssh 세션을 상주시켜서**
없앤다 - 이후 모든 커맨드는 그 연결에 채널만 얹어 곧바로 `su - <user_id>`를 실행한다.
감시 루프가 15초마다 확인해 죽어 있으면 즉시 다시 띄운다.
남는 것은 (d)뿐이고 그 값은 결과의 `duration_ms`로 그대로 보인다.
"""
import sys
import os
import json

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from db import get_pool  # noqa: E402
from config_store import get_config  # noqa: E402
from mcp_caller import (  # noqa: E402
    get_caller, CallerContextMiddleware, tool_description, build_wrapped,
)
from ssh_exec import (  # noqa: E402
    SSH_KEY, ensure_master, run_ssh_as_user, set_output_limit_getter,
    start_master_supervisor, stop_masters,
)
from execution_exec import (  # noqa: E402
    DEFAULT_DENY_CSV, DEFAULT_USER_SCOPE_CSV, build_free_argv, deny_set, user_flag_set,
)
from registry import (  # noqa: E402
    estimate_prompt_tokens, load_registered_sync, set_deny_csv_getter,
    set_user_flags_getter,
)

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("execution-mcp", stateless_http=True, host="0.0.0.0")

_DSN = "execution_db_dsn"


async def _deny_csv() -> str:
    return await get_config("execution_deny_commands", DEFAULT_DENY_CSV)


set_deny_csv_getter(_deny_csv)


async def _user_flags_csv() -> str:
    """다른 사용자를 지목하는 옵션 목록. 비우면 검사하지 않는다(오탐이 나면 여기서 뺀다)."""
    return await get_config("execution_user_scope_flags", DEFAULT_USER_SCOPE_CSV)


set_user_flags_getter(_user_flags_csv)


async def _output_limit() -> int:
    """커맨드 출력을 LLM에 넘길 때의 상한. 매뉴얼·VOC 결과(건당 1500자)와 같은 이유로 필요하다 -
    출력이 그대로 다음 요청 프롬프트에 실린다(#123)."""
    return int(await get_config("execution_result_max_chars", "4000"))


set_output_limit_getter(_output_limit)


async def _login_host() -> str:
    return await get_config("execution_host", "10.0.0.100")


# ------------------------------------------------------------------ 미등록 커맨드 실행
async def run_command(user_id: str, command: str, args: list[str] | None = None,
                      host: str | None = None) -> dict:
    """등록되지 않은 커맨드를 그대로 실행한다(매뉴얼/VOC에서 찾은 것, LLM이 아는 리눅스 명령).

    등록 커맨드는 각각 전용 툴로 이미 노출돼 있으므로 이 툴을 거치지 않는다.
    여기로 오는 것은 아무도 승인하지 않은 문자열이라, 차단 목록을 모든 토큰에 엄격히 적용한다
    (`mpirun -n 4 rm -rf /`, `bash -c "..."` 같은 우회를 막기 위함).
    """
    deny = deny_set(await _deny_csv())
    argv = build_free_argv(command, args, user_id, deny,
                           user_flag_set(await _user_flags_csv()))
    target = (host or "").strip() or await _login_host()
    result = await run_ssh_as_user(target, user_id, argv)
    result["source"] = "미등록(run_command)"
    return result


FREE_TOOLS = {
    "run_command": {
        "handler": run_command,
        "description": (
            "**등록되지 않은** 커맨드를 실행한다(ls·df·du·find·head·nvidia-smi 같은 표준 리눅스 "
            "명령과 매뉴얼에서 찾은 사내 커맨드). 등록된 커맨드는 각각 전용 툴로 나와 있으니 "
            "그 툴을 먼저 쓰고, 목록에 없는 것만 이 툴로 실행한다. "
            "command에는 커맨드 이름만, args에는 인자를 한 칸씩 나눠 넣는다(예: ['-lh']). "
            "본인 홈을 볼 때는 **경로를 넣지 않는다**(항상 본인 홈에서 시작한다). "
            "`/home/...` 경로를 직접 조립하지 않는다. "
            "host는 사용자가 특정 서버를 지목했을 때만 넣는다(기본: 로그인 서버). "
            "파괴적이거나 다른 명령을 대신 실행하는 커맨드는 거부된다."
        ),
        "enabled": True, "required_roles": [], "user_scoped": True, "scope_param": "user_id",
    },
}


# ------------------------------------------------------------------ 상태(활성/역할) 조회
async def _log_execution(tool_name: str, params: dict, status: str, result):
    caller = get_caller()
    pool = await get_pool(_DSN)
    await pool.execute(
        """
        INSERT INTO job_logs (tool_name, params, requested_by, status, result,
                              conversation_id, request_id)
        VALUES ($1, $2::jsonb, $3, $4, $5::jsonb, $6, $7)
        """,
        tool_name,
        json.dumps(params, ensure_ascii=False, default=str),
        caller.get("user_id") or "unknown",
        status,
        json.dumps(result, ensure_ascii=False, default=str) if result is not None else None,
        caller.get("conversation_id"),
        caller.get("request_id"),
    )


async def _tool_state(tool_name: str, default_enabled: bool, default_roles: list) -> tuple:
    """활성 여부와 필요 역할을 **한 행에서 한 번에** 읽는다(콘솔에서 끄면 재시작 없이 즉시 막힘).

    예전에는 같은 행을 두 번 조회했다(enabled 한 번, required_roles 한 번). 툴 호출마다
    DB를 두 번 왕복하는 셈이라, 커맨드 여러 개를 부르는 질문에서는 그만큼 쌓였다.
    """
    if tool_name in FREE_TOOLS:
        return True, []
    pool = await get_pool(_DSN)
    row = await pool.fetchrow(
        "SELECT enabled, required_roles FROM execution_commands WHERE tool_name = $1", tool_name)
    if row is None:
        return default_enabled, list(default_roles or [])
    roles = list(row["required_roles"]) if row["required_roles"] is not None \
        else list(default_roles or [])
    return row["enabled"], roles


# ------------------------------------------------------------------ 툴 구성 (기동 시 1회)
# host_mode와 설명은 LLM 스키마/프롬프트에 영향을 줘서 기동 시에만 반영한다(변경 후 재시작 필요).
REGISTERED, _DROPPED = load_registered_sync(_login_host)
if _DROPPED:
    print(f"[execution-mcp] 등록 커맨드가 상한(execution_tools_max)을 넘어 {_DROPPED}개를 툴로 "
          "노출하지 못했습니다. 설정에서 상한을 올리거나 쓰지 않는 커맨드를 정리하세요 "
          "- 노출되지 않은 커맨드는 run_command로만 실행할 수 있습니다.")

ALL_TOOLS = {**REGISTERED, **FREE_TOOLS}
_CHARS, _TOKENS = estimate_prompt_tokens(
    [e.get("schema_text") or tool_description(n, e) for n, e in ALL_TOOLS.items()])
# 툴 설명은 **매 요청** 프롬프트에 통째로 실린다. 컨텍스트가 32768이라 등록이 늘수록
# 검색 결과·대화 이력에 쓸 자리가 줄어들고, 프리필이 길어져 첫 글자까지의 시간도 늘어난다.
print(f"[execution-mcp] 등록 {len(REGISTERED)}개 · run_command 1개 "
      f"= 툴 {len(ALL_TOOLS)}개 (스키마 {_CHARS:,}자 ≈ {_TOKENS:,}토큰/요청)")
# 어떤 툴이 실제로 노출됐는지 남긴다. 필요한 툴이 꺼져 있으면 에이전트가 쓸 수 없고,
# 그때 답을 지어내는 사고가 났다(#125). 목록을 보면 바로 확인된다.
print(f"[execution-mcp] 노출된 툴: {', '.join(sorted(ALL_TOOLS))}")
if _TOKENS > 8000:
    print(f"[execution-mcp] 경고: 툴 스키마가 요청마다 약 {_TOKENS:,}토큰을 씁니다. 지시문(~5k)과 "
          "합치면 32768 컨텍스트의 절반에 가까워 검색 결과·대화 이력이 밀려나고 응답이 느려집니다. "
          "커맨드 설명을 한 줄로 줄이거나 안 쓰는 커맨드를 정리하세요.")

for _name, _entry in ALL_TOOLS.items():
    mcp.add_tool(
        build_wrapped(_name, _entry, tool_state=_tool_state, log_execution=_log_execution,
                      host_mode=_entry.get("host_mode", "target_server"),
                      login_host=_login_host),
        name=_name,
        description=tool_description(_name, _entry),
    )


async def warm_endpoint(request):
    """`GET /warm` — 로그인 서버로의 상주 마스터가 살아 있는지 확인하고, 없으면 지금 띄운다.

    왜 HTTP로 노출하나: 사용자가 Open WebUI를 새로 열거나 질문을 던지는 시점에
    **연결이 이미 서 있어야** 첫 커맨드가 곧바로 실행된다. 마스터가 없으면 첫 접속에만
    실측 17~25초가 들었다(인증 협상). agent-server가 이 주소를 요청 시작 시 두드린다.
    감시 루프가 이미 돌고 있으므로 평소에는 `already_up: true`로 즉시 끝난다.
    MCP 툴이 아니라 평범한 HTTP 라우트라 LLM 프롬프트에는 실리지 않는다.
    """
    from starlette.responses import JSONResponse

    host = await _login_host()
    if not host:
        return JSONResponse({"ok": False, "reason": "execution_host 미설정"}, status_code=503)
    result = await ensure_master(host)
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


if __name__ == "__main__":
    import uvicorn
    from starlette.routing import Route

    port = int(os.environ.get("MCP_PORT", 8002))
    inner = mcp.streamable_http_app()
    inner.router.routes.append(Route("/warm", warm_endpoint, methods=["GET"]))

    # 서비스가 뜨는 순간 로그인 서버로 root ssh 세션을 **상주**시킨다. 이후 모든 커맨드는
    # 그 연결에 채널만 얹어 곧바로 `su - <user_id>`를 실행한다(접속 비용 0).
    # 감시 루프가 15초마다 살아 있는지 보고, 죽어 있으면 즉시 다시 띄운다.
    async def _start_master():
        # ssh 키가 '파일'인지 먼저 크게 알린다. compose가 없는 경로를 bind mount하면 도커가
        # 그 자리에 **빈 디렉토리**를 만들고, `ssh -i <디렉토리>`는 무조건 인증 실패한다.
        # 실제로 rsync --delete가 서버의 secrets/를 지워서 이 상태가 됐다(#137).
        # 조용히 두면 "커맨드가 전부 권한 오류"로만 보여서 엉뚱한 곳을 파게 된다.
        if SSH_KEY and not os.path.isfile(SSH_KEY):
            print(f"[execution-mcp] !! ssh 키가 파일이 아닙니다: {SSH_KEY} "
                  "→ **모든 커맨드 실행이 인증 실패합니다.** 호스트의 키 파일(.env의 "
                  "SSH_KEY_PATH, 기본 ./secrets/id_ed25519)이 있는지 확인하고 컨테이너를 "
                  "재생성하세요.")
        try:
            host = await _login_host()
        except Exception as e:  # noqa: BLE001
            print(f"[execution-mcp] 로그인 서버 설정을 읽지 못해 마스터를 띄우지 못했습니다: {e}")
            return
        if host:
            result = await ensure_master(host)
            # "ssh 세션이 제대로 열렸는지"를 기동 로그에서 바로 확인할 수 있게 남긴다.
            if result.get("ok"):
                print(f"[execution-mcp] ssh 다중화 마스터 준비 완료({host}). "
                      "첫 커맨드부터 곧바로 실행됩니다.")
            else:
                print(f"[execution-mcp] ssh 마스터를 열지 못했습니다({host}): "
                      f"{result.get('reason')}. 커맨드는 실행되지만 "
                      "매번 새로 접속해 수십 초씩 더 걸립니다. "
                      "scripts/diag-ssh.sh 로 원인을 확인하세요.")
        start_master_supervisor(lambda: get_config("execution_host", host), interval=15)

    inner.add_event_handler("startup", _start_master)
    inner.add_event_handler("shutdown", stop_masters)
    # 공유 비밀값 검사: 같은 망의 누구나 X-User-Id를 붙여 남의 계정으로 실행하는 것을 막는다.
    guarded = CallerContextMiddleware(
        inner, secret_getter=lambda: get_config("mcp_shared_secret", ""))
    uvicorn.run(guarded, host="0.0.0.0", port=port)
