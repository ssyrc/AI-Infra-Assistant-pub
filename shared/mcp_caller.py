"""
실행형 MCP(System/Command)가 공유하는 호출자 컨텍스트 + 안전 실행 래퍼.

- Agent Server가 붙인 호출자 헤더(X-User-Id/-Conversation-Id/-Request-Id/-User-Roles)를
  요청별 ContextVar에 담는다(CallerContextMiddleware).
- build_wrapped(): 화이트리스트 핸들러에 아래를 덧씌운다.
    · user_scoped 툴은 scope_param(기본 user_id)을 LLM 스키마에서 감추고 호출자 신원에서
      강제 주입한다. LLM/사용자가 준 값이 있어도 덮어쓰고, 신뢰된 id가 없으면 거부(fail-closed).
    · enabled/required_roles를 실행 시점에 DB에서 **한 번에** 읽어 검사(콜백 주입).
    · 모든 실행을 감사 로그로 남긴다(콜백 주입). 성공 로그는 응답을 막지 않도록 뒤에서 쓴다.
  DB 접근(대상 DB/테이블)은 각 MCP가 콜백으로 넘겨, 이 모듈은 DB에 독립적이다.
"""
import asyncio
import functools
import hmac
import inspect
import json
from contextvars import ContextVar

_caller: ContextVar[dict] = ContextVar("caller", default={})


async def _deny(send):
    """공유 비밀값이 맞지 않는 호출을 401로 끊는다. 사유를 분명히 적는다 -
    agent-server만 예전 코드로 떠 있어도 이 오류가 나므로, 그 가능성을 함께 알려준다."""
    body = json.dumps({
        "error": "이 MCP는 agent-server에서만 호출할 수 있습니다(공유 비밀값 불일치). "
                 "agent-server가 예전 코드로 떠 있으면 이 오류가 납니다 - "
                 "bash scripts/restart-mounted.sh 로 전부 재시작하세요.",
    }, ensure_ascii=False).encode()
    print("[mcp] 공유 비밀값이 없거나 달라 호출을 거부했습니다(X-Agent-Secret).")
    await send({"type": "http.response.start", "status": 401,
                "headers": [(b"content-type", b"application/json; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})


def get_caller() -> dict:
    return _caller.get() or {}


class CallerContextMiddleware:
    """Agent Server가 붙인 호출자 헤더를 ContextVar에 넣는 ASGI 미들웨어.

    **호출자 인증도 여기서 한다.** MCP는 `X-User-Id`를 그대로 믿고 그 계정 권한으로 커맨드를
    실행한다. 그런데 MCP 포트가 호스트에 열려 있으면, 같은 망의 누구나 그 헤더를 임의로 붙여
    **남의 계정으로 실행**할 수 있다. 그래서 agent-server와 공유하는 비밀값을 확인한다
    (`mcp_shared_secret` — db-init이 무작위로 한 번 심고 양쪽이 같은 DB에서 읽는다).

    비밀값이 설정돼 있지 않으면(구 배포) 통과시키되 기동 시 경고한다 — 인증을 갑자기 강제해
    돌던 서비스를 세우지 않기 위함이다.
    """

    def __init__(self, app, secret_getter=None):
        self.app = app
        self._secret_getter = secret_getter

    async def _expected_secret(self) -> str:
        if self._secret_getter is None:
            return ""
        try:
            return (await self._secret_getter()) or ""
        except Exception:  # noqa: BLE001
            return ""      # 설정을 못 읽었다고 서비스를 세우지는 않는다

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
            expected = await self._expected_secret()
            if expected and not hmac.compare_digest(headers.get("x-agent-secret", ""), expected):
                await _deny(send)
                return
            roles = [r.strip() for r in headers.get("x-user-roles", "").split(",") if r.strip()]
            token = _caller.set({
                "user_id": headers.get("x-user-id"),
                "conversation_id": headers.get("x-conversation-id"),
                "request_id": headers.get("x-request-id"),
                "roles": roles,
            })
            try:
                await self.app(scope, receive, send)
            finally:
                _caller.reset(token)
            return
        await self.app(scope, receive, send)


def tool_description(name: str, entry: dict, overrides: dict | None = None) -> str:
    """LLM에 보일 설명: 콘솔 오버라이드가 있으면 그것을, 없으면 코드 설명을 쓴다.

    내장 커맨드가 없어진 뒤로(모든 커맨드가 콘솔 등록분) 오버라이드 테이블은 쓰지 않는다 —
    등록 커맨드는 설명 자체가 DB 행에 있기 때문이다. 인자는 호환을 위해 남긴다."""
    ov = (overrides or {}).get(name, {})
    return (ov.get("description_override") or "").strip() or entry["description"]


_log_tasks: set = set()   # 감사로그 백그라운드 태스크가 GC로 사라지지 않도록 참조를 보관한다.


def _log_later(log_execution, *args):
    """성공 감사로그는 **응답을 막지 않고** 뒤에서 쓴다.

    성공 경로에서 INSERT를 await하면 그 DB 왕복이 그대로 사용자 대기 시간이 된다.
    커맨드 결과는 이미 손에 있으므로 기다릴 이유가 없다. 실패/차단 경로는 그대로 await한다
    (실행되지 않아 빠르고, 거부 사실은 응답보다 먼저 남는 편이 낫다).
    create_task는 현재 컨텍스트를 복사하므로 호출자 ContextVar(user_id 등)도 그대로 보인다.
    """
    async def _run():
        try:
            await log_execution(*args)
        except Exception as e:  # noqa: BLE001
            print(f"[mcp] 감사로그 기록 실패(무시): {type(e).__name__}: {e}")

    task = asyncio.create_task(_run())
    _log_tasks.add(task)
    task.add_done_callback(_log_tasks.discard)


def build_wrapped(name: str, entry: dict, *, tool_state, log_execution,
                  host_mode: str | None = None, login_host=None):
    """화이트리스트 항목에 권한 검사·감사로그·user_id 강제 주입을 덧씌운 async 함수를 만든다.

    tool_state(name, default_enabled, default_roles) -> (enabled: bool, roles: list)
      활성 여부와 필요 역할을 **한 번에** 돌려준다. 예전에는 두 콜백을 각각 await해서
      툴 호출마다 DB를 두 번 왕복했다 - 같은 행을 두 번 읽는 것이라 합칠 수 있었다.
    log_execution(name, params_dict, status_str, result) -> None
    host_mode: "login_server"면 host 파라미터를 user_id처럼 LLM 스키마에서 숨기고
      login_host()가 돌려주는 값으로 강제 주입한다(기동 시 1회 결정 — 스키마에 영향을 주므로).
    login_host: () -> awaitable[str]. host_mode="login_server"일 때만 필요.
    """
    handler = entry["handler"]
    orig_sig = inspect.signature(handler)
    user_scoped = bool(entry.get("user_scoped", False))
    scope_param = entry.get("scope_param", "user_id")
    hide_host = host_mode == "login_server" and "host" in orig_sig.parameters

    @functools.wraps(handler)
    async def wrapped(*args, **kwargs):
        if user_scoped:
            uid = get_caller().get("user_id")
            if not uid:
                # 신뢰된 호출자 신원이 없으면 실행하지 않는다(남의 자원 접근 방지, fail-closed).
                await log_execution(name, {}, "denied", {"reason": "no authenticated user_id"})
                raise PermissionError(
                    "호출자 사용자 식별자가 없어 실행할 수 없습니다. 관리자에게 문의하세요.")
            # LLM이 위치/키워드로 넣었을 수 있는 값을 무시하고 본인 id로 고정한다.
            args = ()
            kwargs[scope_param] = uid

        if hide_host:
            kwargs["host"] = await login_host()

        try:
            bound = orig_sig.bind(*args, **kwargs)
            bound.apply_defaults()
            params = dict(bound.arguments)
        except Exception:  # noqa: BLE001
            params = {"args": list(args), **kwargs}

        enabled, required = await tool_state(
            name, entry.get("enabled", False), entry.get("required_roles") or [])
        if not enabled:
            await log_execution(name, params, "blocked", {"reason": "disabled by admin"})
            raise PermissionError(f"'{name}' 툴은 관리자 콘솔에서 비활성화되어 있습니다.")

        if required:
            roles = set(get_caller().get("roles", []))
            if not roles.intersection(set(required)):
                msg = f"필요한 역할: {', '.join(required)}"
                await log_execution(name, params, "denied", {"reason": msg})
                raise PermissionError("이 툴을 실행할 권한이 없습니다. " + msg)

        try:
            result = await handler(*args, **kwargs)
            _log_later(log_execution, name, params, "success", result)
            return result
        except PermissionError:
            raise
        except Exception as e:  # noqa: BLE001
            await log_execution(name, params, "error", {"error": str(e)})
            raise

    # 강제 주입되는 파라미터(user_scoped의 scope_param, login_server 모드의 host)는
    # LLM 입력 스키마(시그니처+어노테이션)에서 제거한다.
    hidden_params = set()
    if user_scoped:
        hidden_params.add(scope_param)
    if hide_host:
        hidden_params.add("host")
    if hidden_params:
        reduced = [p for pn, p in orig_sig.parameters.items() if pn not in hidden_params]
        wrapped.__signature__ = orig_sig.replace(parameters=reduced)
        wrapped.__annotations__ = {
            k: v for k, v in getattr(handler, "__annotations__", {}).items() if k not in hidden_params
        }
    return wrapped
