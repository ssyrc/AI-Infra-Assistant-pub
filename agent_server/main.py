"""
Open WebUI가 OpenAI 호환 엔드포인트로 붙는 FastAPI 앱.

세션 전략 (혼합 구조 제거):
Open WebUI는 매 요청에 대화 전체 messages를 보내므로, 이 서버는 **완전 stateless**로 동작한다.
요청마다 세션을 만들고 직전 대화 이력을 주입한 뒤 마지막 사용자 메시지를 실행하고,
응답 후 세션을 정리한다. 대화 격리가 보장되고 replica를 늘려도 세션 공유가 필요 없다.
세션 저장소는 DatabaseSessionService(Postgres)를 쓰되, 요청 종료 시 삭제해 누적을 막는다.

스트리밍:
ADK는 중간 이벤트(부분 응답/툴 호출)를 여러 번 내보내고, 텍스트가 누적된 형태로 올 수 있다.
이미 보낸 접두사를 추적해 실제 증가분(delta)만 전송한다.
"""
import os
import re
import sys
import time
import uuid
import json
import hmac
import asyncio
import traceback
from datetime import datetime, timezone
from contextlib import asynccontextmanager

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field

from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.events import Event
from google.adk.agents.run_config import RunConfig, StreamingMode

STREAMING_RUN_CONFIG = RunConfig(streaming_mode=StreamingMode.SSE)
from google.genai import types

import httpx

from contextlib import nullcontext

try:
    # 트레이스에 user_id/session_id를 붙이는 openinference 컨텍스트(있을 때만 사용).
    from openinference.instrumentation import using_attributes as _using_attributes
except Exception:  # noqa: BLE001
    _using_attributes = None

from config_store import get_config
from db import close_http_client, get_http_client
from memory_store import (
    load_context, format_memory_block, record_turns, maybe_summarize,
    list_user_memory, add_user_memory, delete_user_memory,
)
from service_hub import search_similar_voc
from chart_inline import ChartInliner, charts_base_url
from agent import build_agent, APP_NAME

MAX_MESSAGES = 100
MAX_MESSAGE_CHARS = 32000
MAX_TOTAL_CHARS = 200000

# dev 목업(mock-vllm)일 때는 그 사실이 바로 보이게 실제 모델명을 노출하고,
# 실제 vLLM(운영망 IP)로 붙으면 클라이언트(Open WebUI 등)에는 내부 모델명 대신 브랜드명을 보여준다.
MOCK_LLM_BASE_MARKER = "mock-vllm"
DISPLAY_MODEL_NAME = "AI Infra Assistant"

state: dict = {}


async def _display_model_name() -> str:
    """Open WebUI 등 클라이언트에 노출할 모델 이름. vllm_llm_model은 hot_reload 설정이라
    요청마다 새로 읽어야 설정 탭에서 바꾼 값이 재시작 없이 바로 반영된다."""
    base_url = await get_config("vllm_llm_base_url", "")
    if MOCK_LLM_BASE_MARKER in base_url:
        return await get_config("vllm_llm_model", "qwen3-32b")
    return DISPLAY_MODEL_NAME


async def _close_toolsets(toolsets: list):
    """요청 단위로 만든 MCP toolset을 정리한다(연결 누수 방지)."""
    for ts in toolsets or []:
        try:
            await ts.close()
        except Exception as e:  # noqa: BLE001
            print(f"[agent] toolset 정리 실패(무시): {e}")


# `build_agent`가 toolset을 넣는 순서. 여기서 **도구 이름 → 어느 MCP인지**를 만든다.
_MCP_KINDS = ("manual", "execution", "voc", "chart")

# {도구 이름: MCP 종류}. 기동 시 한 번 채운다(#166). 진행 줄이 "무엇을 하는 중인지"를
# 도구 이름으로 **추측하지 않고** 이 표로 안다 — 등록 커맨드는 콘솔에서 붙인 이름이 그대로
# 툴 이름이라 이름만 봐서는 실행인지 검색인지 알 수 없다.
_TOOL_KIND: dict[str, str] = {}

# {도구 이름: 실제 커맨드}. execution MCP는 툴 설명 끝에 `[head -n {lines} {path}]`처럼
# **실행할 커맨드**를 붙여 준다(`registry._describe`). 그걸 잡아 두면 진행 줄에 도구 이름이
# 아니라 `myquota -h`처럼 **사용자가 아는 커맨드**를 보여줄 수 있다(#166).
_TOOL_CMD: dict[str, str] = {}
_CMD_IN_DESC = re.compile(r"\[([^\[\]]+)\]\s*$")


async def _log_tool_inventory(toolsets: list):
    """기동 시 1회, **모델이 실제로 받는 도구 목록**을 찍고 종류 표를 채운다 (#165·#166).

    "execution mcp가 아예 동작을 안 하는 것 같아"라는 리포트를 지시문·프롬프트 문제로만 보고
    두 턴을 썼다. 그런데 도구가 애초에 안 붙어 있으면 프롬프트를 아무리 고쳐도 소용없다.
    **모델에게 없는 도구는 부를 수 없다** — 그걸 먼저 눈으로 확인할 수 있어야 한다.

    실패해도 기동을 막지 않는다(진단용이다).
    """
    for kind, ts in zip(_MCP_KINDS, toolsets or []):
        try:
            tools = await ts.get_tools()
            names = [getattr(t, "name", "?") for t in tools]
            for tool, n in zip(tools, names):
                _TOOL_KIND[n] = kind
                m = _CMD_IN_DESC.search(getattr(tool, "description", "") or "")
                if kind == "execution" and m:
                    # 자리표시자는 뺀다 - 실제 값은 호출 인자에서 붙인다.
                    base = re.sub(r"\{[^}]*\}", "", m.group(1)).split()
                    if base:
                        _TOOL_CMD[n] = " ".join(base)
            head = ", ".join(names[:12]) + (" …" if len(names) > 12 else "")
            print(f"[agent] {kind} 도구 {len(names)}개: {head}")
        except Exception as e:  # noqa: BLE001
            print(f"[agent] ⚠ {kind} 도구 목록을 못 읽었습니다 — 이 MCP의 도구는 모델에게 "
                  f"보이지 않습니다: {type(e).__name__}: {e}")


async def require_api_key(request: Request):
    """`/v1/*` 호출을 API 키로 인증한다(설정된 경우에만).

    **왜 필요한가**: 이 서버는 `X-OpenWebUI-User-Email` 헤더를 그대로 믿고 그 계정 권한으로
    커맨드를 실행한다. `/v1/agent/query`는 아예 본문의 `user_id`를 쓴다. 포트가 호스트에
    열려 있으므로, 인증이 없으면 같은 망의 누구나 헤더만 바꿔 **남의 계정으로 실행**할 수 있다.

    Open WebUI는 연결(Connections)에 넣은 키를 `Authorization: Bearer <key>`로 보낸다.
    콘솔 설정의 `agent_api_key`에 같은 값을 넣으면 그때부터 인증이 강제된다.
    비워 두면 검사하지 않는다(기존 배포를 갑자기 세우지 않기 위함) - 대신 기동 로그에 경고.
    """
    expected = (await get_config("agent_api_key", "") or "").strip()
    if not expected:
        return
    header = request.headers.get("authorization", "")
    token = header[7:].strip() if header[:7].lower() == "bearer " else ""
    if not token:
        token = request.headers.get("x-api-key", "").strip()
    if not hmac.compare_digest(token, expected):
        raise HTTPException(401, "API 키가 없거나 올바르지 않습니다. Open WebUI 연결"
                                 "(Connections)의 API 키와 콘솔의 agent_api_key를 맞추세요.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 기동 시 1회: 설정/ MCP 주소 유효성 검증. 실제 실행 에이전트와 모델명은 요청마다 새로 가져온다.
    _agent, _model_name, toolsets = await build_agent()
    await _log_tool_inventory(toolsets)
    await _close_toolsets(toolsets)
    session_db_dsn = await get_config("agent_session_db_dsn")
    if not session_db_dsn:
        raise RuntimeError("agent_session_db_dsn이 설정되지 않았습니다.")
    state["session_service"] = DatabaseSessionService(db_url=session_db_dsn)
    # 인증 상태를 기동 로그에 남긴다. 꺼져 있으면 "누구나 남의 계정으로 실행 가능"이므로
    # 조용히 넘어가면 안 된다.
    if (await get_config("agent_api_key", "") or "").strip():
        print("[agent] /v1/* API 키 인증이 켜져 있습니다.")
    else:
        print("[agent] !! /v1/* 에 인증이 없습니다. 이 포트에 닿을 수 있는 누구나 "
              "헤더만 바꿔 다른 사용자 권한으로 커맨드를 실행할 수 있습니다. "
              "콘솔 설정의 agent_api_key를 Open WebUI 연결의 API 키와 같게 넣으세요.")
    try:
        yield
    finally:
        await close_http_client()


app = FastAPI(lifespan=lifespan)


class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None


class ChatCompletionRequest(BaseModel):
    model: str | None = None
    messages: list[ChatMessage] = Field(default_factory=list)
    stream: bool = False
    user: str | None = None


# --- ssh 연결 예열 ------------------------------------------------------------------
# 사용자가 Open WebUI를 새로 열거나(=/v1/models 호출) 질문을 던지는 시점에 로그인 서버로의
# ssh 마스터가 **이미 서 있어야** 첫 커맨드가 곧바로 실행된다. 없으면 첫 접속에만 17초가
# 들었다(실측, 인증 협상). Execution MCP의 /warm은 이미 서 있으면 아무것도 하지 않는다.
# 응답을 기다리지 않는다(예열은 부가 작업이고, 실패해도 커맨드는 평소대로 직접 접속한다).
_warm_tasks: set = set()


def warm_execution_host():
    async def _run():
        try:
            url = (await get_config("execution_mcp_url", "") or "").strip()
            if not url:
                return
            base = url.rstrip("/")
            for suffix in ("/mcp", "/sse"):        # MCP 엔드포인트 경로를 떼고 /warm으로
                if base.endswith(suffix):
                    base = base[: -len(suffix)]
                    break
            client = await get_http_client()
            await client.get(f"{base}/warm", timeout=20)
        except Exception as e:  # noqa: BLE001
            print(f"[agent] ssh 예열 요청 실패(무시): {type(e).__name__}: {e}")

    task = asyncio.create_task(_run())
    _warm_tasks.add(task)
    task.add_done_callback(_warm_tasks.discard)


@app.get("/health")
async def health():
    warm_execution_host()
    return {"status": "ok", "model": await _display_model_name()}


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def list_models():
    # Open WebUI가 페이지를 열거나 새로고침할 때 부르는 엔드포인트다. 여기서 예열하면
    # 사용자가 첫 질문을 타이핑하는 동안 ssh 세션이 준비된다.
    warm_execution_host()
    return {"object": "list", "data": [{"id": await _display_model_name(), "object": "model"}]}


def _text_of(content) -> str:
    """OpenAI 형식은 content가 문자열 또는 파트 배열일 수 있다. 텍스트만 추출한다."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                if p.get("type") == "text" and isinstance(p.get("text"), str):
                    parts.append(p["text"])
            elif isinstance(p, str):
                parts.append(p)
        return "\n".join(parts)
    return str(content)


def _validate(req: ChatCompletionRequest, model_name: str) -> list[tuple[str, str]]:
    """요청을 검증하고 (role, text) 목록을 돌려준다."""
    if not req.messages:
        raise HTTPException(400, "messages가 비어 있습니다.")
    if len(req.messages) > MAX_MESSAGES:
        raise HTTPException(413, f"메시지가 너무 많습니다(최대 {MAX_MESSAGES}개).")

    if req.model and req.model != model_name:
        raise HTTPException(400, f"지원하지 않는 모델입니다: {req.model}")

    normalized: list[tuple[str, str]] = []
    total = 0
    for m in req.messages:
        text = _text_of(m.content)
        if len(text) > MAX_MESSAGE_CHARS:
            raise HTTPException(413, f"메시지가 너무 깁니다(최대 {MAX_MESSAGE_CHARS}자).")
        total += len(text)
        normalized.append((m.role, text))
    if total > MAX_TOTAL_CHARS:
        raise HTTPException(413, f"대화 전체 길이가 너무 깁니다(최대 {MAX_TOTAL_CHARS}자).")

    # system 메시지는 에이전트 instruction이 담당하므로 대화 이력에서 제외
    convo = [(r, t) for r, t in normalized if r in ("user", "assistant")]
    if not convo:
        raise HTTPException(400, "user 또는 assistant 메시지가 필요합니다.")
    if convo[-1][0] != "user":
        raise HTTPException(400, "마지막 메시지는 user여야 합니다.")
    if not convo[-1][1].strip():
        raise HTTPException(400, "마지막 사용자 메시지가 비어 있습니다.")
    return convo


async def _trim_history(history: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """대화 이력을 글자 수 예산 안으로 줄인다(오래된 턴부터 버림).

    Open WebUI는 대화 전체를 매 요청에 실어 보낸다. 검색 결과가 붙은 긴 답변이 몇 번
    쌓이면 이력만으로 컨텍스트가 가득 차고, 실제로 32768토큰을 넘겨
    ContextWindowExceededError가 났다. 최근 턴이 가장 중요하므로 뒤에서부터 채운다.
    """
    try:
        budget = int(await get_config("history_max_chars", "8000"))
    except (TypeError, ValueError):
        budget = 8000
    if budget <= 0:
        return history
    kept, used = [], 0
    for role, text in reversed(history):
        t = text or ""
        if used + len(t) > budget and kept:
            break
        kept.append((role, t))
        used += len(t)
    if len(kept) < len(history):
        print(f"[agent] 대화 이력 {len(history)}턴 중 최근 {len(kept)}턴만 사용"
              f"(예산 {budget}자)")
    return list(reversed(kept))


async def _create_session(user_id: str, history: list[tuple[str, str]]) -> str:
    session_id = str(uuid.uuid4())
    svc = state["session_service"]
    session = await svc.create_session(app_name=APP_NAME, user_id=user_id, session_id=session_id)
    # 세션 객체는 **한 번만** 읽는다. 예전에는 이력 한 턴마다 get_session()을 다시 불렀는데,
    # get_session은 그때까지 쌓인 이벤트를 전부 다시 읽어 오므로 턴 수의 제곱으로 늘어난다
    # (10턴이면 DB 왕복 20번 + 매번 커지는 페이로드). append_event가 메모리의 session에도
    # 이벤트를 더해 주므로 다시 읽을 이유가 없다. 이건 사용자가 첫 글자를 보기 전의 지연이다.
    for role, text in await _trim_history(history):
        adk_role = "user" if role == "user" else "model"
        event = Event(author=adk_role,
                      content=types.Content(role=adk_role, parts=[types.Part(text=text)]))
        await svc.append_event(session=session, event=event)
    return session_id


async def _cleanup_session(user_id: str, session_id: str):
    """요청 단위 세션이므로 응답 후 삭제해 세션 테이블 누적을 막는다."""
    try:
        await state["session_service"].delete_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 세션 정리 실패(무시): {e}")


def _sse(request_id: str, model: str, delta: str, finish: bool = False) -> str:
    payload = {
        "id": request_id, "object": "chat.completion.chunk",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0,
                     "delta": {} if finish else {"content": delta},
                     "finish_reason": "stop" if finish else None}],
    }
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _event_text(event) -> str:
    if not event.content or not event.content.parts:
        return ""
    return "".join(p.text or "" for p in event.content.parts)


# 이름의 성격(검색/실행/조회)만 보고 사람 말로 바꿔서, 무엇을 하는 중인지만 알린다.
# 관리자가 콘솔에서 새 도구를 추가해도 규칙이 그대로 적용되도록 이름 매칭은 부분 문자열로 한다.


def _first_text_arg(args: dict) -> str:
    for v in (args or {}).values():
        if isinstance(v, str) and v.strip():
            return v.strip()[:40]
    return ""


def _exec_command_text(args: dict) -> str:
    """실행 툴의 인자에서 **실제로 돌아갈 커맨드 한 줄**을 만든다.

    예전에는 첫 문자열 인자만 보여줘서 `· 'ls' 실행하는 중`이었다. 그러면 `-A`가 빠져
    숨김 파일이 안 나온 건지, 출력이 잘린 건지 사용자가 구분할 수 없다. 전부 보여준다.
    """
    parts = []
    cmd = args.get("command")
    if isinstance(cmd, str) and cmd.strip():
        parts.append(cmd.strip())
    extra = args.get("args")
    if isinstance(extra, str):
        parts.append(extra.strip())
    elif isinstance(extra, list):
        parts += [str(a) for a in extra]
    # 등록 커맨드는 command 대신 타입 붙은 인자로 온다(lines/path 등). 값만 이어 붙인다.
    if not parts:
        parts = [str(v) for k, v in (args or {}).items()
                 if k not in ("host", "user_id") and v not in (None, "", [])]
    return " ".join(parts)[:80]


def _action_phrase(name: str, args: dict) -> str:
    """도구 호출을 사용자에게 보여줄 한 줄로 바꾼다. **도구 이름은 절대 노출하지 않는다.**

    무엇을 하는 중인지는 **어느 MCP의 도구인가**로 정한다(#166). 이름으로 추측하지 않는다 —
    등록 커맨드는 콘솔에서 붙인 이름이 그대로 툴 이름이라(`s2_phd_list`, `myquota` …)
    이름만 봐서는 실행인지 검색인지 알 수 없고, 실제로 `· 확인하는 중`만 뜨고 있었다(#165).

        · '접속 오류' 관련 매뉴얼 검색 중
        · '접속 오류' 관련 과거 VOC 이력 검색 중
        · `myquota -h` 실행 중
    """
    q = _first_text_arg(args)
    quoted = f"'{q}' " if q else ""
    kind = _TOOL_KIND.get(name or "")

    if kind == "execution":
        # 등록 커맨드는 인자만 오므로(`{"option": "-h"}`) 그것만 보여주면 `-h 실행 중`이
        # 된다. 툴 설명에서 잡아 둔 커맨드를 앞에 붙여 `myquota -h`로 만든다.
        full = " ".join(x for x in (_TOOL_CMD.get(name or ""),
                                    _exec_command_text(args)) if x).strip()
        return f"`{full}` 실행 중" if full else "커맨드 실행 중"
    if kind == "manual":
        return f"{quoted}관련 매뉴얼 검색 중"
    if kind == "voc":
        return f"{quoted}관련 과거 VOC 이력 검색 중"
    if kind == "chart":
        return "차트 그리는 중"

    # 표에 없는 도구(기동 뒤에 등록된 커맨드 등)는 이름으로 어림한다.
    low = (name or "").lower()
    if "run" in low or "exec" in low:
        full = _exec_command_text(args)
        return f"`{full}` 실행 중" if full else "커맨드 실행 중"
    if "manual" in low or "document" in low:
        return f"{quoted}관련 매뉴얼 검색 중"
    if "voc" in low:
        return f"{quoted}관련 과거 VOC 이력 검색 중"
    if "chart" in low:
        return "차트 그리는 중"
    # 여기까지 왔으면 어느 MCP인지도 이름 힌트도 없다. **실행이라고 단정하지 않는다** —
    # 검색 도구의 인자를 커맨드처럼 보여주면 하지도 않은 실행을 한 것처럼 보인다.
    return f"{quoted}확인 중".strip()


def _unwrap_result(resp):
    """MCP 응답에서 안쪽 결과를 꺼낸다.

    한두 겹 감싸여 오거나(`{"result": {...}}`) JSON 문자열로 올 수 있다. 안쪽을 못 찾으면
    실행 툴인데도 "확인 완료"로 보여서 성공/실패를 알 수 없다. 상태 문장과 원문 블록이
    **같은 규칙**으로 풀어야 해서 함수로 뺐다(어긋나면 한쪽만 결과를 못 찾는다).
    """
    r = resp
    for _ in range(3):
        if isinstance(r, str):
            try:
                r = json.loads(r)
                continue
            except (ValueError, TypeError):
                break
        if isinstance(r, dict) and "exit_code" not in r and "stdout" not in r:
            inner = r.get("result", r.get("content"))
            if isinstance(inner, (dict, str)):
                r = inner
                continue
        break
    return r


def _result_phrase(name: str, resp) -> str:
    """도구 결과를 짧은 상태 문장으로 요약한다."""
    r = _unwrap_result(resp)
    if isinstance(r, list):
        return f"{len(r)}건 찾음" if r else "찾은 내용 없음"
    if isinstance(r, dict):
        # 실행 툴이면 '어디서 누구 권한으로 몇 초' 걸렸는지 함께 보여준다.
        # 출력만 보고는 진짜 실행됐는지, 의도한 서버가 맞는지 알 수 없고, "느리다"는 리포트가
        # 왔을 때 어느 커맨드가 느린지 사용자가 화면에서 바로 짚어 줄 수 있다.
        bits = [str(x) for x in (r.get("ip"), r.get("as_user")) if x]
        if isinstance(r.get("duration_ms"), int):
            bits.append(f"{r['duration_ms'] / 1000:.1f}초")
        where = f" ({' · '.join(bits)})" if bits else ""
        # 출력이 잘렸으면 **사용자에게 직접** 알린다. 이 줄은 LLM을 거치지 않으므로,
        # 모델이 "일부만 표시" 안내를 빼먹어도 화면에는 남는다.
        if r.get("truncated"):
            where += f" ⚠ 출력 {r.get('total_lines')}줄 중 {r.get('shown_lines')}줄만"
        elif isinstance(r.get("total_lines"), int) and r["total_lines"] > 1:
            # **잘리지 않았을 때도 줄 수를 보여준다.** 모델이 답변에서 행을 조용히 줄여도
            # 사용자가 이 숫자와 눈으로 대조할 수 있다 - 예전에는 잘렸을 때만 표시해서,
            # 22줄만 보이는 답을 받고도 우리가 자른 건지 모델이 자른 건지 알 수 없었다(#146).
            where += f" · {r['total_lines']}줄"
        if r.get("error"):
            return f"실패{where} — {str(r['error'])[:60]}"
        if "exit_code" in r:
            return (f"완료{where}" if r.get("exit_code") == 0
                    else f"실패{where}(종료코드 {r['exit_code']})")
        return "확인 완료"
    if r is None:
        return "찾은 내용 없음"
    text = str(r).strip()
    return (text[:50] + "…") if len(text) > 50 else (text or "완료")


class _RawOutputs:
    """실행 결과 **원문**을 모아 답변 뒤에 그대로 붙인다 (#150).

    왜 필요한가: 모델이 목록형 출력을 **조용히 줄이는** 일이 반복됐다(132줄 중 22줄만 표시).
    지시문으로 세 번 막아 봤지만 지시문은 확률이다 — 게다가 지시문이 길어질수록 개별 규칙의
    준수율은 떨어진다. 진행 줄이 그랬듯, 사용자가 **반드시 봐야 하는 것은 LLM을 거치지 않고**
    붙인다. 모델이 무엇을 쓰든 원문은 화면에 남는다.

    여러 줄일 때만 붙인다. 한두 줄짜리 출력은 모델 답변에 이미 다 들어가 있어서 중복이다.
    """

    def __init__(self, min_lines: int, max_chars: int):
        self.min_lines = max(2, min_lines)
        self.max_chars = max_chars
        self.items: list[dict] = []

    def observe(self, event):
        for fr in (event.get_function_responses() or []):
            r = _unwrap_result(fr.response)
            if not isinstance(r, dict) or "exit_code" not in r:
                continue                       # 실행 툴이 아니다(검색 결과 등)
            out = (r.get("stdout") or "").rstrip()
            if not out or len(out.splitlines()) < self.min_lines:
                continue
            self.items.append({
                "command": r.get("command") or "",
                "ip": r.get("ip") or "",
                "as_user": r.get("as_user") or "",
                "total_lines": r.get("total_lines"),
                "truncated": bool(r.get("truncated")),
                "stdout": out,
            })

    @staticmethod
    def _fence(text: str) -> str:
        """본문에 백틱 울타리가 들어 있어도 깨지지 않게 더 긴 울타리를 쓴다.
        (실행 결과에 ``` 가 들어 있으면 코드블록이 중간에 닫혀 나머지가 마크다운으로 샌다.)"""
        longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
        return "`" * max(3, longest + 1)

    def block(self) -> str:
        if not self.items:
            return ""
        parts = ["\n\n---\n\n**실행 결과 원문**"]
        for it in self.items:
            meta = " · ".join(x for x in (it["ip"], it["as_user"]) if x)
            if isinstance(it["total_lines"], int):
                meta += f" · {it['total_lines']}줄"
            if it["truncated"]:
                meta += " (뒷부분 잘림)"
            head = f"`{it['command']}`" if it["command"] else "실행 결과"
            body = it["stdout"]
            if self.max_chars > 0 and len(body) > self.max_chars:
                # 원문 블록에도 상한은 둔다. 없으면 한 답변이 수십만 자가 될 수 있다.
                kept, used = [], 0
                for line in body.splitlines():
                    if used + len(line) + 1 > self.max_chars and kept:
                        break
                    kept.append(line)
                    used += len(line) + 1
                body = "\n".join(kept) + f"\n…(원문 표시 상한 {self.max_chars:,}자에서 자름)"
            fence = self._fence(body)
            parts.append(f"\n\n{head}{' — ' + meta if meta else ''}\n\n"
                         f"{fence}text\n{body}\n{fence}")
        return "".join(parts)


async def _int_config(key: str, default: int) -> int:
    try:
        return int(await get_config(key, str(default)))
    except (TypeError, ValueError):
        return default


async def _make_raw_outputs():
    """설정을 읽어 수집기를 만든다. 꺼져 있으면 None(호출 지점이 조용히 건너뛴다)."""
    if not _mem_on(await get_config("execution_raw_output", "true")):
        return None
    return _RawOutputs(await _int_config("execution_raw_output_min_lines", 2),
                       await _int_config("execution_raw_output_max_chars", 20000))


async def _raw_output_summary(raw, question: str) -> str:
    """원문 블록 뒤에 붙일 짧은 요약. **LLM을 한 번 더 부른다**(기본 꺼짐).

    모델의 첫 답변은 원문보다 **앞**에 오므로, 긴 원문을 스크롤한 뒤 결론을 다시 보고 싶을 때
    쓴다. 실패하면 조용히 빈 문자열 - 요약 때문에 답변이 막히면 안 된다.
    """
    if not raw or not raw.items:
        return ""
    if not _mem_on(await get_config("execution_raw_output_summary", "false")):
        return ""
    base = await get_config("vllm_llm_base_url", "")
    model = await get_config("vllm_llm_model", "")
    if not base or not model:
        return ""
    joined = "\n\n".join(f"[{it['command']}]\n{it['stdout']}" for it in raw.items)[:8000]
    prompt = ("아래는 사용자 질문과 실제 실행 결과다. 결과를 **2~3줄로** 요약하라. "
              "결과에 없는 내용을 덧붙이지 말고, 숫자는 결과 그대로 쓴다. 서론 없이 본문만.\n\n"
              f"질문: {question}\n\n실행 결과:\n{joined}")
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{base.rstrip('/')}/chat/completions",
                json={"model": model, "messages": [{"role": "user", "content": prompt}],
                      "temperature": 0.2, "max_tokens": 300})
            resp.raise_for_status()
            text = ((resp.json().get("choices") or [{}])[0]
                    .get("message", {}) or {}).get("content") or ""
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 원문 요약 실패(무시): {type(e).__name__}: {e}")
        return ""
    # 추론형 모델이 `<think>…</think>`를 앞에 붙일 수 있다. 요약에는 필요 없다.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()
    return f"\n\n**요약**\n\n{text}" if text else ""


# 답변에 들어가면 **치명적인** 값들. 조회 결과에 없으면 지어낸 것이다.
#   · IP: 사용자가 그 주소로 접속을 시도한다. 틀린 IP는 곧바로 사고다(실제로 없는 서버 IP를
#         만들어 안내한 적이 있다).
#   · 절대 경로: 없는 경로를 안내하면 사용자가 헤맨다(#125의 `/home/ops_assistant`).
_IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_PATH_RE = re.compile(r"(?<![\w.])/(?:[\w.@+-]+/){1,}[\w.@+-]*")
# 문서 안내를 시작하는 문구. 매뉴얼을 검색하지 않았다면 이 블록 자체가 지어낸 것이다.
_GUIDE_MARKERS = ("자세한 내용은 다음 문서", "가이드 문서:", "가이드 위치:")


# 답변에서 계정처럼 보이는 토큰(`말.말`). `pii._ACCOUNT_RE`는 점 앞에 숫자를 요구해
# `other.user`을 놓치므로, 노출 차단용으로는 더 넓게 본다.
_ACCOUNTISH_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9_-]{1,15}\.[A-Za-z][A-Za-z0-9_-]{1,20}\b")
# 계정이 아닌 것: 파일 확장자와 도메인 꼬리. 여기 없는 확장자가 오면 줄 하나가 빠질 뿐이고,
# 반대 방향(남의 계정 노출)보다 훨씬 가벼운 실패다.
_NOT_ACCOUNT_SUFFIX = {
    "py", "sh", "md", "yml", "yaml", "json", "log", "txt", "csv", "tsv", "sql",
    "html", "js", "css", "svg", "png", "jpg", "tar", "gz", "whl", "cfg", "conf",
    "ini", "env", "example", "bak", "tmp", "lock", "toml", "xml", "pdf", "xlsx",
    "com", "net", "org", "io", "kr", "local", "internal", "dev", "test",
}


class _AnswerGuard:
    """근거 없는 답변을 **내보내지 않는다** (#155).

    #154에서는 경고 문구를 덧붙였는데, 사용자가 분명히 거절했다 —
    "저런 그대로 믿지마세요 문구를 넣지말라고. **아예 지어내지 말라고.**
     우리 매뉴얼에 없거나 그 어떤 db에서 확인할 수 없는거면 **운영팀에 문의하라**고 하라고."

    맞는 지적이다. 틀린 IP를 보여주면서 "믿지 마세요"를 붙이는 것은 답이 아니다.
    근거가 없으면 **그 답변을 버리고** 운영팀 문의 안내로 바꾼다.

    스트리밍에서도 바꿀 수 있어야 하므로, 이 검사가 켜져 있으면 본문을 **끝까지 모았다가**
    한 번에 내보낸다(진행 줄은 그대로 흘러서 사용자는 기다리는 동안 무엇을 하는지 본다).
    """

    def __init__(self, enabled: bool, question: str, env_text: str = "",
                 intake: str = "", user_id: str = ""):
        self.enabled = enabled
        self.intake = (intake or "").strip()
        self.user_id = (user_id or "").strip().lower()
        self.searched_manual = False
        self.corpus = [question or "", env_text or ""]

    def _foreign_accounts(self, answer: str) -> list:
        """답변에 든 **남의 계정**을 찾는다 (#171).

        VOC 검색 결과는 `pii.mask_record`가 이미 가린다. 남는 구멍은 **질문에 적힌 남의
        계정을 답변이 그대로 되뇌는 것**이다 — 실제로 `{남의계정}으로 접속이 불가합니다`에
        `귀하의 계정({남의계정})으로 …`라고 답했다. 근거에 있느냐로는 못 막는다.
        질문 자체가 근거이기 때문이다. **호출자 본인 계정이 아니면 전부 막는다.**
        """
        # 호출자를 모르면(테스트·내부 호출) 판단할 수 없다. 그때는 막지 않는다 —
        # 전부 막으면 본인 계정이 든 정상 답변까지 사라진다.
        if not self.user_id:
            return []
        # `pii._ACCOUNT_RE`보다 **넓게** 잡는다. 그건 점 앞에 숫자가 있는 형태만 보므로
        # `other.user` 같은 계정을 놓친다(마스킹에도 같은 구멍이 있다 — 별건으로 고친다).
        # 여기서는 파일명·도메인을 접미사로 걸러 내고 나머지 `말.말`을 전부 계정으로 본다.
        # **거짓 양성(줄 하나가 빠짐)보다 거짓 음성(남의 계정 노출)이 훨씬 나쁘다.**
        return [m for m in dict.fromkeys(_ACCOUNTISH_RE.findall(answer or ""))
                if m.strip().lower() != self.user_id
                and m.rsplit(".", 1)[-1].lower() not in _NOT_ACCOUNT_SUFFIX]

    def seed_rag(self, manual_hits: list, voc_hits: list = ()):
        """선검색(`_rag_context`)으로 이미 확보한 근거를 등록한다.

        모델이 툴을 부르지 않아도 매뉴얼·VOC를 본 것과 같으므로, 여기 실린 값은 근거로
        인정한다. **매뉴얼이 0건이면 검색한 것으로 치지 않는다** — 그 상태에서 문서를
        안내하면 지어낸 것이다.
        """
        if manual_hits:
            self.searched_manual = True
        for r in list(manual_hits or []) + list(voc_hits or []):
            try:
                self.corpus.append(json.dumps(r, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                self.corpus.append(str(r))

    def observe(self, event):
        if not self.enabled:
            return
        for fc in (event.get_function_calls() or []):
            if "manual" in (fc.name or "").lower():
                self.searched_manual = True
        for fr in (event.get_function_responses() or []):
            try:
                self.corpus.append(json.dumps(fr.response, ensure_ascii=False, default=str))
            except Exception:  # noqa: BLE001
                self.corpus.append(str(fr.response))

    def _ungrounded(self, answer: str) -> list:
        haystack = "\n".join(self.corpus)
        out = []
        for pat in (_IP_RE, _PATH_RE):
            for m in dict.fromkeys(pat.findall(answer)):
                if m not in haystack:
                    out.append(m)
        return out

    def _intake_line(self) -> str:
        return f"\n\n접수 경로: {self.intake}" if self.intake else ""

    def fallback(self, reason: str) -> str:
        """**남길 게 하나도 없을 때만** 쓰는 답변. 근거 있는 문장이 하나라도 남으면
        `review`가 그것을 살린다(사용자 지시: 매뉴얼 기반 확인 사항을 먼저 안내하고,
        그 다음에 운영팀 문의)."""
        print(f"[agent] 근거 없는 답변을 차단했습니다: {reason}")
        return ("문의하신 내용은 매뉴얼과 과거 사례에서 확인되지 않았습니다.\n"
                "정확하지 않은 정보를 드리지 않기 위해 답변을 드리지 않습니다. "
                "운영팀에 문의해 주세요." + self._intake_line())

    @property
    def hold(self) -> bool:
        """본문을 흘리지 않고 모아 두어야 하는가.

        검사가 켜져 있으면 **참**이다. 이미 화면에 나간 글자는 도로 거둘 수 없으므로,
        갈아 끼우려면 끝까지 모으는 수밖에 없다. 진행 줄(도구 실행 표시)은 그대로 흘러서
        사용자는 기다리는 동안 무슨 일이 벌어지는지 계속 본다.
        본문이 한 번에 나오는 게 싫으면 설정에서 `answer_grounding_check`를 끈다 —
        대신 지어낸 값이 그대로 나간다.
        """
        return self.enabled

    def review(self, answer: str) -> str:
        """내보낼 최종 본문. 지어낸 값이 든 **줄만** 덜어내고 나머지는 살린다 (#156).

        #155에서는 값 하나가 근거에 없으면 답변을 통째로 버렸다. 그게 지나쳤다 —
        사용자 지적: "바로 운영팀 확인이 필요하다고 하는데, **manual_db 기반으로 우선
        사용자가 확인해야 할 사항들 먼저 쭉 가이드를 하고, 그 후에** 운영팀한테 문의하라고
        해야지." 매뉴얼에서 확인된 점검 목록 다섯 줄 중 한 줄에 지어낸 IP가 있다고 해서
        나머지 네 줄까지 버리면, 사용자는 스스로 할 수 있는 일을 못 하게 된다.

        지어낸 값이 사용자에게 **보이지 않는다**는 원칙은 그대로다. 그 줄만 지운다.
        남는 게 없으면 그때 `fallback`으로 간다.
        """
        if not self.enabled or not (answer or "").strip():
            return answer
        if not self.searched_manual and any(k in answer for k in _GUIDE_MARKERS):
            return self.fallback("매뉴얼을 검색하지 않고 문서를 안내함")
        # 남의 계정은 **근거 유무와 무관하게** 막는다(질문 자체가 근거가 되어 버린다).
        bad = self._foreign_accounts(answer) + self._ungrounded(answer)
        if not bad:
            return answer

        kept = [ln for ln in answer.split("\n") if not any(b in ln for b in bad)]
        body = "\n".join(kept).strip()
        # 표제·불릿 기호만 남은 껍데기는 답이 아니다. 실질 내용이 남았는지로 판단한다.
        substantive = len(re.sub(r"[\s#\-*>|`0-9.]", "", body)) >= 20
        if not substantive:
            return self.fallback("조회 결과에 없는 값: " + ", ".join(bad[:8]))
        print(f"[agent] 근거 없는 줄을 덜어냈습니다: {', '.join(bad[:8])}")
        return (body + "\n\n확인되지 않은 내용은 제외했습니다. "
                "더 필요하시면 운영팀에 문의해 주세요." + self._intake_line())


async def _make_grounding(question: str, user_id: str = ""):
    """설정을 읽어 검사기를 만든다. 꺼져 있으면 검사하지 않는 빈 객체.

    `build_agent`가 프롬프트에 넣어 주는 '이 환경의 값'은 우리가 준 사실이므로 근거로 인정한다
    (그러지 않으면 로그인 서버 IP를 그대로 안내한 답변이 매번 경고를 받는다)."""
    on = _mem_on(await get_config("answer_grounding_check", "true"))
    intake = await get_config("voc_intake_guide", "")
    env = " ".join(str(x) for x in (
        user_id,
        await get_config("execution_host", ""),
        await get_config("openwebui_public_url", ""),
        intake,
    ) if x)
    return _AnswerGuard(on, question, env, intake, user_id=user_id)


class _Pace:
    """요청 하나가 어디에 시간을 썼는지 로그로 남긴다.

    "느리다"는 리포트가 올 때마다 원인을 추측해 왔다(ssh 키·호스트 키·TTY — 셋 다 틀렸고
    실제 원인은 타임아웃이었다, #69). 매 요청 아래 네 숫자를 찍어 두면 다음부터는 로그 한 줄로
    갈린다: 전체 / 첫 글자까지 / 도구 호출 횟수 / 그중 커맨드 실행에 실제로 쓴 시간.
    나머지(전체 - 커맨드)는 곧 LLM이 생각한 시간이다.
    """

    def __init__(self, request_id: str, user_id: str):
        self.t0 = time.monotonic()
        self.request_id = request_id
        self.user_id = user_id
        self.tool_calls = 0
        self.tool_ms = 0
        self.first_text_at = None
        # 세션 생성 + 장기기억 조회 + MCP toolset 4개 연결까지, **LLM을 부르기 전** 시간.
        # "요청마다 MCP 세션을 새로 만들어서 느리다"는 지적이 있었는데, 재지 않고는 알 수 없다.
        # 이 값이 작으면 그 지적은 이 환경에서 사실이 아니다.
        self.prep_at = None

    def mark_ready(self):
        if self.prep_at is None:
            self.prep_at = time.monotonic() - self.t0

    def observe(self, event):
        self.tool_calls += len(event.get_function_calls() or [])
        for fr in (event.get_function_responses() or []):
            resp = fr.response
            if not isinstance(resp, dict):
                continue
            inner = resp.get("result") if isinstance(resp.get("result"), dict) else resp
            if isinstance(inner, dict) and isinstance(inner.get("duration_ms"), int):
                self.tool_ms += inner["duration_ms"]

    def mark_first_text(self):
        if self.first_text_at is None:
            self.first_text_at = time.monotonic() - self.t0

    def done(self):
        total = time.monotonic() - self.t0
        first = f"{self.first_text_at:.1f}초" if self.first_text_at is not None else "-"
        prep = f"{self.prep_at:.1f}초" if self.prep_at is not None else "-"
        print(f"[agent] {self.request_id} 완료 {total:.1f}초 (준비 {prep} · 첫 글자 {first} · "
              f"도구 {self.tool_calls}회 · 커맨드 실행 {self.tool_ms / 1000:.1f}초 · {self.user_id})")


def _tool_status_lines(event) -> str:
    """진행 상황을 사람이 읽는 한 줄로 만든다(도구 이름·인자 원문은 노출하지 않는다).

    답변 앞에 그대로 흘려보낸다(예전에는 <details> 블록으로 감쌌는데, 클라이언트에 따라
    태그가 그대로 보여서 걷어냈다). 사용자는 "지금 무엇을 하는 중인지"만 알게 되고
    내부 도구 구성은 드러나지 않는다.
    """
    lines = []
    for fc in (event.get_function_calls() or []):
        lines.append(f"· {_action_phrase(fc.name, fc.args)}")
    for fr in (event.get_function_responses() or []):
        lines.append(f"· {_result_phrase(fr.name, fr.response)}")
    return "\n".join(lines)


class _StreamDedup:
    """ADK 스트리밍 이벤트를 '사용자에게 새로 보낼 증가분'으로 바꾼다.

    왜 필요한가: ADK는 한 메시지를 partial 이벤트 여러 개로 흘려보낸 뒤, **같은 내용을 담은
    최종 이벤트를 한 번 더** 보낸다. 이걸 그대로 흘리면 답변이 두 번씩 출력된다.
    까다로운 점 두 가지를 모두 처리한다.
      1) partial의 text가 '델타'인 경우와 '지금까지 누적'인 경우가 둘 다 있다.
      2) 툴 호출이 끼면 메시지가 여러 개 생기고, 메시지마다 누적이 처음부터 다시 시작한다.
    그래서 partial 플래그로 메시지 경계를 잡고(최종 이벤트 = 경계), 경계마다 누적을 리셋한다.
    """

    def __init__(self):
        self.cur = ""            # 현재 메시지에서 이미 보낸 텍스트
        self.saw_partial = False
        self.full = ""           # 이번 턴 전체 텍스트(메모리 저장·완성 응답용)

    def feed(self, event) -> str:
        text = _event_text(event)
        if not text:
            return ""
        if getattr(event, "partial", False):
            self.saw_partial = True
            delta = text[len(self.cur):] if text.startswith(self.cur) else text
            self.cur += delta
        else:
            if not self.saw_partial:
                delta = text                      # partial 없이 최종만 온 메시지
            elif text.startswith(self.cur):
                delta = text[len(self.cur):]      # 대개 "" (이미 다 보냄)
            else:
                delta = ""                        # 이미 보낸 내용의 재전송 -> 버린다
            self.cur, self.saw_partial = "", False   # 메시지 경계
        self.full += delta
        return delta



# --- 차트 인라인 -------------------------------------------------------------------
# Chart MCP는 `chart://<id>` 표시자만 돌려준다(프롬프트 예산 때문). 사용자에게 내보낼 때
# 여기서 data URI로 바꿔 넣으므로, 이미지용 포트를 열거나 외부 주소를 설정할 필요가 없다.
# 대화 이력에는 표시자가 그대로 남는다(다음 요청 프롬프트가 부풀지 않게).
async def _fetch_chart_svg(chart_id: str) -> str | None:
    base = charts_base_url(await get_config("chart_mcp_url", ""))
    if not base:
        return None
    client = await get_http_client()
    r = await client.get(f"{base}/charts/{chart_id}.svg", timeout=10)
    if r.status_code != 200:
        print(f"[chart] {chart_id} 응답 {r.status_code}")
        return None
    return r.text


def _chart_inliner() -> ChartInliner:
    return ChartInliner(_fetch_chart_svg)



# --- 오류 문구 ----------------------------------------------------------------------
# 컨텍스트 초과는 사용자가 고칠 수 있는 문제다. litellm 스택트레이스를 그대로 보여주면
# 무엇을 해야 하는지 알 수 없다(실제로 59,360토큰 오류가 그렇게 노출됐다 - #123).
_CONTEXT_ERROR_MARKERS = ("ContextWindowExceeded", "maximum context length",
                          "reduce the length of the input")
# 툴 호출 인자를 JSON으로 못 읽은 경우. vLLM의 tool-call 파서(hermes)가 스트리밍 중에
# 조각난/깨진 JSON을 내보내면 여기로 떨어진다. 사용자가 고칠 수 있는 게 없고 대개 재시도로 풀린다.
# 원문(`Expecting value: line 1 column 11 (char 10)`)은 사용자에게 아무 의미가 없다.
_JSON_ERROR_MARKERS = ("Expecting value:", "Expecting ',' delimiter",
                       "Expecting property name", "Unterminated string starting at",
                       "JSONDecodeError")


async def _friendly_error(e: Exception) -> str:
    text = str(e)
    if any(m in text for m in _CONTEXT_ERROR_MARKERS):
        used = re.search(r"request has (\d+) input tokens", text)
        limit = re.search(r"maximum context length is (\d+) tokens", text)
        detail = ""
        if used and limit:
            detail = f" ({int(used.group(1)):,} / {int(limit.group(1)):,} 토큰)"
        # "운영팀에 알려주세요"로 끝내면 어디로 알려야 하는지가 빠진다. 접수 경로는 관리자가
        # 콘솔(voc_intake_guide)에 넣어 둔 값이 있으니 그걸 그대로 안내한다.
        intake = (await get_config("voc_intake_guide", "") or "").strip()
        where = f" 계속 발생하면 여기로 알려주세요: {intake}" if intake else \
                " 계속 발생하면 운영팀에 알려주세요."
        return ("한 번에 처리할 수 있는 양을 넘었습니다" + detail + ".\n"
                "출력이 많은 커맨드를 여러 번 실행했거나 대화가 길어진 경우입니다. "
                "새 대화에서 다시 물어보시거나, 조회 범위를 좁혀 주세요"
                "(예: 서버 하나만, 기간을 줄여서)." + where)
    if any(m in text for m in _JSON_ERROR_MARKERS):
        return ("도구를 호출하는 형식이 깨져서 이번 요청을 끝내지 못했습니다. "
                "같은 질문을 한 번 더 보내주세요(대개 다시 하면 됩니다). "
                "반복되면 질문을 조금 짧게 나눠서 물어봐 주세요.")
    return f"오류가 발생했습니다: {text}"


def _is_toolcall_json_error(e: Exception) -> bool:
    """ADK가 스트리밍 툴 호출 인자를 JSON으로 못 읽은 경우인지.

    **google-adk 1.22.1의 결함이다.** `lite_llm.py`의 스트리밍 분기가 툴 호출 조각을
    `index = chunk.index or fallback_index`로 모으는데, 파이썬에서 `0`은 거짓이라
    **index 0을 '없음'으로 취급**한다. vLLM(hermes 파서)이 같은 호출의 조각에 index를
    0 → 1로 바꿔 보내면 인자가 두 통에 쪼개져 담기고, 각각은 잘린 JSON이 된다.
    그걸 `_message_to_generate_content_response`가 `json.loads(...)`로 그냥 파싱해서
    (try/except 없음) 요청 전체가 죽는다. 실제 오류가
    `Expecting value: line 1 column 11 (char 10)` = 인자가 `{"lines": `에서 잘린 모양이다.

    우리가 라이브러리를 고칠 수는 없지만, **스트리밍을 끄면 이 경로 자체를 안 탄다**
    (논스트리밍 분기는 litellm이 완성된 인자 문자열을 한 번에 준다).
    """
    return isinstance(e, ValueError) and any(m in str(e) for m in _JSON_ERROR_MARKERS)


async def _run_with_toolcall_recovery(runner, user_id, session_id, new_message,
                                      run_config, *, history):
    """스트리밍으로 돌리되, 위 ADK 결함에 걸리면 **논스트리밍으로 한 번 더** 돌린다.

    재시도 시에는 세션을 새로 만든다 - 실패한 실행이 세션에 중간 이벤트를 남겼을 수 있고,
    그대로 다시 돌리면 모델이 같은 자리에서 또 걸린다.
    사용자에게는 답이 조금 늦게(한 덩어리로) 도착할 뿐, 요청이 죽지 않는다.
    """
    if run_config is None:
        async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                            new_message=new_message):
            yield event
        return

    started_output = False
    try:
        async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                            new_message=new_message, run_config=run_config):
            started_output = True
            yield event
        return
    except Exception as e:  # noqa: BLE001
        if not _is_toolcall_json_error(e):
            raise
        print(f"[agent] 스트리밍 툴 호출 파싱 실패(google-adk 결함) → 논스트리밍으로 재시도: {e}")
        if started_output:
            # 이미 사용자에게 흘려보낸 게 있으면 앞부분이 중복된다. 그래도 답이 없는 것보다 낫다.
            print("[agent] (일부 출력이 이미 나간 뒤라 답변 앞부분이 겹칠 수 있습니다)")

    retry_session = await _create_session(user_id, history)
    try:
        async for event in runner.run_async(user_id=user_id, session_id=retry_session,
                                            new_message=new_message):
            yield event
    finally:
        await _cleanup_session(user_id, retry_session)


def _trace_ctx(user_id: str, session_id: str | None, source: str | None):
    """Langfuse 트레이스에 user_id/session_id(대화)를 붙여 사용자별로 묶이게 한다.
    openinference가 없거나 트레이싱이 꺼져 있으면 무해한 no-op이다."""
    if _using_attributes is None:
        return nullcontext()
    md = {"source": source} if source else None
    return _using_attributes(user_id=user_id or "anonymous",
                             session_id=session_id or "", metadata=md)


def _to_os_identity(raw: str) -> str:
    """OS 계정 신원으로 정규화한다.
    - 이메일 형태(user@corp.com)면 로컬파트(@ 앞)만 사용한다 -> 리눅스 계정명으로 매핑.
    - 리눅스 계정명은 소문자라 소문자로 맞춘다(Open WebUI 이메일에 대문자가 섞여 있어도 매핑되게).
    - 형식 검증/특권 계정 거부는 실행 직전 shared/ssh_exec.validate_user가 담당한다."""
    ident = (raw or "").strip()
    if "@" in ident:
        ident = ident.split("@", 1)[0].strip()
    return ident.lower()


def _caller_from_request(request: Request, req: ChatCompletionRequest) -> tuple[str, str, str]:
    """호출자 신원을 Open WebUI가 전달하는 헤더에서 읽는다.
    Open WebUI에서 ENABLE_FORWARD_USER_INFO_HEADERS=true여야 이 헤더들이 온다.
    OS 계정 매핑에 쓰려고 이메일(로컬파트)을 우선한다. Open WebUI의 User-Id는 보통 UUID라
    리눅스 계정과 맞지 않기 때문이다. body의 user 필드는 대개 비어 있어 헤더를 우선한다.
    (agent-server는 내부망에서 Open WebUI만 접근하므로 이 헤더를 신뢰한다.)"""
    h = request.headers
    raw = (h.get("x-openwebui-user-email")
           or h.get("x-openwebui-user-name")
           or h.get("x-openwebui-user-id")
           or req.user
           or "anonymous")
    user_id = _to_os_identity(raw)[:128] or "anonymous"
    role = (h.get("x-openwebui-user-role") or "").strip()   # 예: "admin" | "user"
    chat_id = h.get("x-openwebui-chat-id") or ""
    return user_id, role, chat_id


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)])
async def chat_completions(req: ChatCompletionRequest, request: Request):
    warm_execution_host()          # 답을 만드는 동안 ssh 세션이 준비되게(응답을 막지 않는다)
    model_name = await _display_model_name()
    convo = _validate(req, model_name)
    user_id, user_role, chat_id = _caller_from_request(request, req)
    request_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    pace = _Pace(request_id, user_id)

    history, (_, last_text) = convo[:-1], convo[-1]
    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=last_text)])

    # Open WebUI 경로도 '우리' 장기 메모리를 user_id 단위로 공유한다(외부 agent와 동일 저장소).
    # 대화 이력은 이미 messages에 있으므로 최근 턴은 주입하지 않고, 증류된 장기기억만 주입한다.
    conv = chat_id or _auto_conv(user_id)
    mem_enabled = _mem_on(await get_config("memory_enabled", "true"))
    show_tools = _mem_on(await get_config("show_tool_activity", "true"))
    memory_block = await _longterm_memory_block(user_id, conv, last_text) if mem_enabled else None

    # 요청 단위로 에이전트를 만들어 호출자 헤더를 MCP에 전달한다.
    # System MCP는 X-User-Id로 user_scoped 툴(예: 본인 job 조회)의 user_id를 강제 주입하고,
    # X-User-Roles로 required_roles를 검사한다(Open WebUI 역할이 그대로 전달됨).
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv,
        "X-Request-Id": request_id,
        "X-User-Roles": user_role,
    }
    raw = await _make_raw_outputs()
    ground = await _make_grounding(last_text, user_id)
    prepared: dict = {"toolsets": [], "found": (0, 0)}

    async def _prepare():
        """선검색 → 에이전트 구성. **스트리밍에서는 진행 줄을 먼저 내보낸 뒤** 부른다 (#163).

        선검색은 사용자 로그에서 2.9~3.0초가 걸렸는데, 그동안 화면에 아무것도 없었다.
        검색이 도구 호출이 아니라 우리 코드라 진행 줄이 붙을 자리가 없었기 때문이다(#155).
        """
        rag_block, manual_hits, voc_hits = await _rag_context(last_text, history)
        extra = ((memory_block or "") + rag_block) if rag_block else memory_block
        agent, _model, toolsets = await build_agent(caller_headers, extra)
        prepared["toolsets"] = toolsets
        prepared["found"] = (len(manual_hits), len(voc_hits))
        ground.seed_rag(manual_hits, voc_hits)
        pace.mark_ready()      # 여기까지가 LLM을 부르기 전 준비 시간
        return Runner(agent=agent, app_name=APP_NAME,
                      session_service=state["session_service"])

    if not req.stream:
        runner = await _prepare()
        final_text = ""
        try:
            with _trace_ctx(user_id, conv, "openwebui"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    pace.observe(event)
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
            pace.done()
        # 이력에는 **표시자 그대로** 저장한다(data URI가 들어가면 다음 프롬프트가 부푼다).
        _bg_persist(user_id, conv, "openwebui", last_text, final_text, mem_enabled)
        # 원문 블록은 **이력에 저장한 뒤** 붙인다(다음 프롬프트가 부풀지 않게).
        # 근거 없는 답변은 여기서 **버려진다**(모델을 거치지 않는다).
        body = await _chart_inliner().whole(ground.review(final_text))
        if raw:
            body += raw.block() + await _raw_output_summary(raw, last_text)
        return JSONResponse({
            "id": request_id, "object": "chat.completion",
            "created": int(time.time()), "model": model_name,
            "choices": [{"index": 0,
                         "message": {"role": "assistant", "content": body},
                         "finish_reason": "stop"}],
        })

    stream_mode = _mem_on(await get_config("llm_streaming", "true"))

    async def event_stream():
        dedup = _StreamDedup()
        charts = _chart_inliner()
        in_think = False
        try:
            # 선검색은 도구 호출이 아니라 우리 코드라 진행 줄이 붙을 자리가 없었다(#155).
            # 그래서 2~3초 동안 화면이 비어 있었다. 여기서 **먼저 한 줄 내보내고** 검색한다.
            if show_tools:
                yield _sse(request_id, model_name, "· 매뉴얼·과거 사례 검색하는 중\n")
                in_think = True
            runner = await _prepare()
            if show_tools:
                mh, vh = prepared["found"]
                yield _sse(request_id, model_name,
                           f"· 매뉴얼 {mh}건 · 과거 사례 {vh}건 찾음\n")

            run_config = STREAMING_RUN_CONFIG if stream_mode else None
            with _trace_ctx(user_id, conv, "openwebui"):
                async for event in _run_with_toolcall_recovery(
                        runner, user_id, session_id, new_message, run_config,
                        history=history):
                    if await request.is_disconnected():
                        print("[agent] 클라이언트 연결 종료, 스트리밍 중단")
                        break
                    pace.observe(event)
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    if show_tools:
                        status = _tool_status_lines(event)
                        if status:
                            in_think = True
                            yield _sse(request_id, model_name, status + "\n")
                    delta = dedup.feed(event)
                    if delta:
                        pace.mark_first_text()
                        if ground.hold:
                            continue          # 검사 후 한 번에 내보낸다(#155)
                        # 차트 표시자가 델타 경계에 걸쳐 쪼개져 올 수 있어 안전한 부분만 흘린다.
                        out = await charts.feed(delta)
                        if out:
                            if in_think:      # 진행 줄과 답변 사이만 한 줄 띄운다
                                yield _sse(request_id, model_name, "\n")
                                in_think = False
                            yield _sse(request_id, model_name, out)

            # 모아 둔 본문을 검사한 뒤 내보낸다. 근거가 없으면 운영팀 문의 안내로 바뀐다.
            tail = (await charts.whole(ground.review(dedup.full)) if ground.hold
                    else await charts.flush())    # 붙들고 있던 꼬리 마무리
            if tail:
                if in_think:
                    yield _sse(request_id, model_name, "\n")
                    in_think = False
                yield _sse(request_id, model_name, tail)
            if in_think:
                yield _sse(request_id, model_name, "\n")
            # **모델 답변이 끝난 뒤** 원문을 붙인다. 모델이 행을 줄여도 전체가 화면에 남는다.
            if raw:
                block = raw.block()
                if block:
                    yield _sse(request_id, model_name, block)
                    summary = await _raw_output_summary(raw, last_text)
                    if summary:
                        yield _sse(request_id, model_name, summary)
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            # **스택트레이스를 남긴다.** 예전에는 메시지만 찍어서
            # `Expecting value: line 1 column 11 (char 10)` 같은 오류가 어디서 났는지
            # (툴 인자 JSON 파싱인지, 응답 파싱인지) 알 수 없었다. 사용자에게는 그대로
            # 보여주지 않고 로그에만 남긴다.
            print(f"[agent] 스트리밍 오류: {type(e).__name__}: {e}\n{traceback.format_exc()}")
            yield _sse(request_id, model_name, f"\n\n[{await _friendly_error(e)}]")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
            pace.done()
            _bg_persist(user_id, conv, "openwebui", last_text, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# ================================================================= Agent-to-agent API + 장기 메모리
def _mem_on(raw: str | None) -> bool:
    return (raw or "true").strip().lower() == "true"


def _auto_conv(user_id: str) -> str:
    """conversation_id가 없을 때 시간(UTC 일 단위)으로 스레드를 만든다.
    같은 날 같은 사용자의 요청은 한 대화로 이어져 최근 턴·요약이 동작한다."""
    return f"auto-{user_id}-{datetime.now(timezone.utc):%Y%m%d}"


async def _longterm_memory_block(user_id: str, conversation_id: str | None, query: str):
    """증류된 장기기억만 시스템 지시문 블록으로 반환한다(최근 턴은 주입하지 않음)."""
    try:
        tk = int(await get_config("memory_top_k", "5"))
    except (TypeError, ValueError):
        tk = 5
    ctx = await load_context(user_id, conversation_id, query, 0, tk)
    return format_memory_block(ctx["longterm"]) or None


def _retrieval_query(question: str, history: list | None = None,
                     max_chars: int = 600) -> str:
    """검색에 쓸 질의. `history`를 주면 **직전 사용자 발화를 앞에 붙인다** (#156).

    이어지는 질문은 그 문장만으로는 검색이 안 된다 —
    "그러면 슈퍼컴 접속 못 하는거 아니야?"에는 검색할 명사가 거의 없다.
    직전 사용자 발화를 붙이면 "login server 접속 …" 같은 앞 문맥이 질의에 들어와 같은
    문서를 찾아낸다.

    **다만 이건 1차 질의가 아니다**(#163). 병합을 항상 하면 앞 턴이 다른 주제일 때 질의가
    오염되어 **앞 질문의 답을 찾아온다.** `_rag_context`는 `history=None`으로 먼저 부르고,
    0건일 때만 `history`를 넘겨 다시 부른다.

    **답변(assistant)이 아니라 사용자 발화만** 붙인다. 답변까지 넣으면 지어낸 내용이
    질의에 섞여 그 방향으로 검색이 끌려간다(틀린 답 → 틀린 근거 → 틀린 답의 고리).
    """
    q = (question or "").strip()
    prev = ""
    for role, text in reversed(history or []):
        if role == "user" and (text or "").strip():
            prev = text.strip()
            break
    merged = f"{prev}\n{q}" if prev else q
    return merged[-max_chars:] if len(merged) > max_chars else merged


async def _none():
    """`asyncio.gather`의 자리를 채우는 빈 코루틴(이미 찾은 쪽은 다시 검색하지 않는다)."""
    return None


async def _embed_once(query: str):
    """선검색이 쓸 질의 벡터를 **한 번만** 만든다 (#165).

    매뉴얼과 VOC는 같은 질의를 쓰는데 각자 임베딩을 불렀다 — 임베딩 서버 왕복이 두 번이다.
    실패하면 `None`을 주고, 각 검색은 키워드 축으로 알아서 떨어진다(지금과 같다).
    """
    try:
        from db import embed_text
        return await embed_text(query)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] 선검색 임베딩 실패, 키워드 검색으로 진행: {type(e).__name__}: {e}")
        return None


async def _search_manual_for(query: str, top_k: int, vec=None):
    try:
        from manual_search import search_manual_chunks
        _mode, results = await search_manual_chunks(query, top_k, with_neighbors=True,
                                                    vec=vec)
        return results
    except Exception as e:  # noqa: BLE001
        # 매뉴얼 DB나 임베딩이 죽어도 답변 자체는 계속돼야 한다(모델이 툴로 다시 시도한다).
        print(f"[agent] 매뉴얼 선검색 실패(무시): {type(e).__name__}: {e}")
        return []


async def _search_voc_for(query: str, top_k: int, vec=None):
    try:
        from voc_search import search_voc_records
        return await search_voc_records(query, top_k, vec=vec)
    except Exception as e:  # noqa: BLE001
        print(f"[agent] VOC 선검색 실패(무시): {type(e).__name__}: {e}")
        return []


def _manual_block(results: list) -> str:
    lines = ["\n\n# 이번 질문에 대한 매뉴얼 검색 결과"]
    if not results:
        # "없습니다"로 끝내면 모델이 곧바로 포기한다(#162). 우리가 만든 질의가 사용자 문장
        # 그대로라 검색에 불리할 수 있으므로, **모델이 직접 다시 찾을 것**을 먼저 시킨다.
        lines.append("**이 질의로는 못 찾았습니다.** 우리가 쓴 질의는 사용자 문장 그대로라 "
                     "검색어로는 불리할 수 있습니다. 이 시스템의 사용법·절차·정책·구성에 대한 "
                     "질문이라면, **핵심 명사만 남긴 다른 표현으로 `search_manual`을 직접 "
                     "불러 보세요**(예: 표현 지시나 군더더기를 뺀 두세 낱말). "
                     "그렇게 해도 안 나오면 그때 확인되지 않는다고 답하고, "
                     "매뉴얼에 없는 내용을 지어내지 마세요.")
        return "\n".join(lines)
    lines += ["아래는 사내 매뉴얼에서 이미 찾아 둔 근거입니다. **답변은 이 내용으로 만드세요.**",
              "문서를 안내할 때 `문서 위치`는 **한 글자도 바꾸지 말고 그대로** 옮겨 적습니다.",
              # 선검색이 매 질문마다 근거를 넣어 주니, 모델이 **실행해야 하는 질문에도**
              # 이 블록으로 답해 버린다(#163). `내 홈스토리지 경로 알려줘`에 매뉴얼 안내만
              # 하고 커맨드를 안 부른 것이 그것이다. 유혹이 생기는 자리에서 경계를 긋는다.
              "**이 근거는 '이 회사에서는 어떻게 하는가'에만 씁니다.** 이 사용자의 값"
              "(내 경로·내 용량·내 job처럼 계정마다 다른 값)을 묻는 질문이면 매뉴얼은 답이 "
              "될 수 없습니다 — 여기서 답을 만들지 말고 **실행해서 확인하세요.** "
              "매뉴얼은 그때 '어떤 커맨드로 보는지'를 알려 줄 뿐입니다."]
    for i, r in enumerate(results, 1):
        loc = (r.get("guide_location") or "").strip()
        doc = (r.get("guide_document") or "").strip()
        lines.append(f"\n## 매뉴얼 근거 {i}")
        if loc:
            lines.append(f"- 문서 위치: {loc}")
        if doc:
            lines.append(f"- 문서 이름: {doc}")
        if r.get("section_title"):
            lines.append(f"- 섹션: {r['section_title']}")
        lines.append(f"- 내용:\n{(r.get('chunk_text') or '').strip()}")
    return "\n".join(lines)


_VOC_HANDLED = {
    "user": "사용자가 직접 해결",
    "operator": "운영자가 확인·조치",
}
# 판정이 없거나 `unknown`인 건. **취급은 운영자 건과 같이** 하되(남이 해 준 조치를 사용자에게
# 시키는 쪽이 반대 오류보다 나쁘다), **표기는 따로 한다**(#158). 조치 내용이 없어서 못 가른
# 것을 "운영자가 확인·조치"라고 적으면, 우리가 모르는 것을 안다고 프롬프트에 쓰는 셈이다.
_VOC_UNKNOWN = "판정 불가 — 답변에 조치 내용이 없어 누가 처리했는지 가리지 못함"


def _voc_block(results: list) -> str:
    lines = ["\n\n# 이번 질문에 대한 과거 사례(VOC) 검색 결과"]
    if not results:
        lines.append("**이 질의로는 못 찾았습니다.** 증상·오류에 대한 문의라면 **핵심 낱말만 "
                     "남긴 다른 표현으로 `search_voc`를 직접 불러 보세요.** 그렇게 해도 "
                     "안 나오면 과거에 이런 문의가 있었던 것처럼 쓰지 마세요.")
        return "\n".join(lines)
    lines.append("아래는 실제로 접수됐던 문의와 그 답변입니다. 지금 질문과 **정말 같은 건인지** "
                 "보고 쓰세요(증상만 비슷하고 대상이 다르면 쓰지 않습니다).")
    # 사용자가 직접 해결한 건이 아닌 사례가 섞여 있을 때만 붙인다(#158). 전부 `user`면
    # 이 지침은 답과 무관한 잡음이고 프롬프트 예산만 먹는다.
    if any((r.get("handled_by") or "").strip().lower() != "user" for r in results):
        lines.append(
            "**`사용자가 직접 해결`이 아닌 사례를 쓸 때**: 그 조치를 사용자에게 시키지 말고, "
            "거기 적힌 원인을 **여러 가능성 중 하나로** 제시하세요. "
            "`이번 건의 원인은 ~입니다`라고 단정하면 안 됩니다 — 이번 건의 원인은 확인된 적이 "
            "없습니다. 사례에서 나온 원인이 여럿이면 **여럿을 다 적고**, 원인마다 사용자가 해 볼 "
            "수 있는 것을 매뉴얼에서 찾아 함께 답니다. 운영팀 접수는 **맨 마지막**입니다.")
    for i, r in enumerate(results, 1):
        raw = (r.get("handled_by") or "").strip().lower()
        lines.append(f"\n## 과거 사례 {i} (처리: {_VOC_HANDLED.get(raw, _VOC_UNKNOWN)})")
        lines.append(f"- 문의: {(r.get('question') or '').strip()}")
        lines.append(f"- 답변: {(r.get('answer') or '').strip()}")
    return "\n".join(lines)


async def _rag_context(question: str, history: list | None = None) -> tuple[str, list, list]:
    """**매 질문마다 매뉴얼과 과거 사례를 먼저 검색해 프롬프트에 넣는다** (#155·#156).

    사용자 지시: "사용자 문의가 들어오면 무조건!!! 제발!!!! manual_mcp, voc_mcp 로 rag 한 후에
    답변 생성하길 바람."

    지시문으로 네 번 시켰지만 모델은 자기가 아는 일반지식으로 답할 수 있다고 판단하면 툴을
    부르지 않았다. **부를지 말지를 모델에게 맡기지 않는다** — 우리가 먼저 검색해서 결과를
    프롬프트에 넣는다. 그러면 "검색을 안 했다"는 실패 자체가 성립하지 않는다.
    (#155에서 매뉴얼만 했는데, VOC도 같은 이유로 안 불리고 있었다 — voc_db에 있는 질문을
     그대로 물었는데 엉뚱한 답이 나왔다.)

    검색 경로는 각 MCP가 쓰는 것과 **같은 것 하나뿐**이라 결과가 갈릴 일이 없다.
    툴(`search_manual`·`search_voc`)은 그대로 남아서, 첫 결과로 부족하면 모델이 다른 질의로
    다시 찾는다.

    두 검색은 **동시에** 돈다 — 합이 아니라 둘 중 느린 쪽이 지연이다.

    Returns:
        (프롬프트에 붙일 블록, 매뉴얼 결과, VOC 결과)
        결과는 근거 검사(_AnswerGuard)의 근거로도 쓴다.
    """
    if not _mem_on(await get_config("rag_prefetch", "true")):
        return "", [], []
    query = _retrieval_query(question, history)
    if not query.strip():
        return "", [], []
    try:
        mk = int(await get_config("manual_prefetch_top_k", "3"))
    except (TypeError, ValueError):
        mk = 3
    try:
        vk = int(await get_config("voc_prefetch_top_k", "3"))
    except (TypeError, ValueError):
        vk = 3

    # **이번 질문만으로 먼저 찾는다.** 직전 발화를 붙인 질의는 0건일 때만 쓴다 (#163).
    #
    # #156에서 직전 발화를 앞에 붙인 것은 "그러면 접속 못 하는거 아니야?"처럼 검색할 명사가
    # 없는 이어지는 질문 때문이었다. 그건 지금도 맞다. 틀린 것은 그걸 **항상** 한 것이다.
    #
    # 실제로 이렇게 났다(사용자 로그):
    #   q='GPU, cpu 서버별 위치 궁금해\nAA, BB, CC이 뭐야'  →  매뉴얼 3건
    # 이번 질문은 **약어가 뭐냐**인데 찾아온 3건은 앞 질문(서버 위치)의 것이었다. 0건이 아니라
    # **엉뚱한 3건**이라 #162의 재시도(0건일 때만)가 아예 돌지 않았고, 모델은 약어 풀이가
    # 프롬프트에 없으니 지어냈다.
    #
    # 순서를 뒤집으면 두 경우가 다 산다:
    #   · `AA, BB, CC이 뭐야`      → 그 자체로 명사가 있다. 1차에서 약어 풀이를 찾는다.
    #   · `그러면 접속 못 하는거…`  → 1차 0건 → 2차(직전 발화 병합)가 접속 가이드를 찾는다.
    t0 = time.monotonic()
    bare = _retrieval_query(question, None)
    merged = _retrieval_query(question, history)

    vec = await _embed_once(bare)
    manual_hits, voc_hits = await asyncio.gather(
        _search_manual_for(bare, max(1, mk), vec),
        _search_voc_for(bare, max(1, vk), vec),
    )
    if merged != bare and not (manual_hits and voc_hits):
        rvec = await _embed_once(merged)
        retry_m, retry_v = await asyncio.gather(
            _search_manual_for(merged, max(1, mk), rvec) if not manual_hits else _none(),
            _search_voc_for(merged, max(1, vk), rvec) if not voc_hits else _none(),
        )
        if retry_m or retry_v:
            print(f"[agent] 선검색 재시도(직전 발화 포함): 매뉴얼 {len(retry_m or [])}건 · "
                  f"VOC {len(retry_v or [])}건")
        manual_hits = manual_hits or retry_m or []
        voc_hits = voc_hits or retry_v or []

    ms = int((time.monotonic() - t0) * 1000)
    print(f"[agent] 선검색 매뉴얼 {len(manual_hits)}건 · VOC {len(voc_hits)}건 ({ms}ms)")

    block = _manual_block(manual_hits) + _voc_block(voc_hits)
    block += ("\n\n위 근거로 답을 만들 수 없으면 **다른 표현으로** `search_manual` 또는 "
              "`search_voc`를 다시 부르세요. 그래도 없으면 지어내지 말고 확인되지 않는다고 "
              "답합니다.")
    return block, manual_hits, voc_hits
async def _summarize_turns(turns: list[dict]) -> list[str]:
    """대화 턴들에서 '이 사용자에 대해 기억할' 사실/선호를 vLLM으로 뽑아 한 줄씩 반환한다."""
    base = await get_config("vllm_llm_base_url")
    model = await get_config("vllm_llm_model", "qwen3-32b")
    convo = "\n".join(f"{t['role']}: {t['content']}" for t in turns)[:8000]
    # **인프라 사용법·절차·값은 절대 기억하지 않는다.** 예전에는 "사실/선호/맥락"을 뽑게 했더니
    # "CPU 노드에서 스크래치는 /scratch/… 를 쓴다" 같은 절차가 장기기억에 들어갔고, 나중에
    # "GPU에서 스크래치 사용법"을 물었을 때 의미검색이 그 CPU 항목을 끌어와 시스템 지시문에
    # 주입했다 -> CPU 답을 그대로 냈다(#122). 그런 내용은 매번 매뉴얼을 다시 검색해야 한다.
    prompt = (
        "다음 대화에서 **이 사용자 자신에 대한** 정보만 한국어로 간결히 0~5개 뽑아줘.\n"
        "뽑을 것: 소속·담당 업무, 사용하는 계정/서버, 반복되는 관심 주제, 답변 형식 선호.\n"
        "**절대 뽑지 말 것**: 사용법·절차·명령어·경로·옵션·설정값 등 '어떻게 하는가'에 해당하는 "
        "모든 내용(그건 매뉴얼에서 매번 다시 찾아야 하는 것이라 기억하면 틀린 답의 원인이 된다). "
        "일회성 잡담, 일반 상식, 비밀번호 같은 민감정보도 제외한다.\n"
        "각 항목은 한 줄로, 접두어 없이 문장만. 뽑을 게 없으면 빈 줄만 출력.\n\n"
        f"대화:\n{convo}"
    )
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base.rstrip('/')}/chat/completions",
            json={"model": model, "messages": [{"role": "user", "content": prompt}],
                  "temperature": 0.2, "max_tokens": 400},
        )
        resp.raise_for_status()
        data = resp.json()
        text = ((data.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
    out = []
    for line in text.splitlines():
        s = line.strip().lstrip("-•*").strip()
        # 선두 번호(1. 2)) 제거
        while s[:1].isdigit():
            s = s[1:].lstrip(".) ").strip()
        if s:
            out.append(s)
    return out[:7]


class AgentQueryIn(BaseModel):
    user_id: str
    message: str
    conversation_id: str | None = None
    source: str | None = None
    roles: list[str] | None = None
    use_memory: bool = True
    stream: bool = False


async def _memory_context(user_id: str, conversation_id: str | None, query: str):
    """(history[(role,text)], extra_instruction|None) 반환."""
    try:
        rt = int(await get_config("memory_recent_turns", "8"))
        tk = int(await get_config("memory_top_k", "5"))
    except (TypeError, ValueError):
        rt, tk = 8, 5
    ctx = await load_context(user_id, conversation_id, query, rt, tk)
    hist = [("user" if t["role"] == "user" else "assistant", t["content"]) for t in ctx["recent"]]
    return hist, (format_memory_block(ctx["longterm"]) or None)


_bg_tasks: set = set()   # 백그라운드 태스크가 GC로 사라지지 않도록 참조를 보관한다.


def _bg_persist(user_id, conversation_id, source, message, answer, mem_enabled):
    """응답 후 백그라운드로 턴 저장 + (임계 도달 시) 요약 승격.
    메모리가 꺼져 있으면(use_memory=false 또는 memory_enabled=false) 아무것도 저장하지 않는다."""
    async def _run():
        try:
            await record_turns(user_id, conversation_id, source,
                               [("user", message), ("assistant", answer)])
            if conversation_id:
                try:
                    every = int(await get_config("memory_summarize_every", "12"))
                    ttl = int(await get_config("memory_ttl_days", "180"))
                except (TypeError, ValueError):
                    every, ttl = 12, 180
                await maybe_summarize(user_id, conversation_id, _summarize_turns, every, ttl)
        except Exception as e:  # noqa: BLE001
            print(f"[agent] 메모리 저장/요약 실패(무시): {e}")
    if answer and mem_enabled:
        task = asyncio.create_task(_run())
        _bg_tasks.add(task)
        task.add_done_callback(_bg_tasks.discard)


@app.post("/v1/agent/query", dependencies=[Depends(require_api_key)])
async def agent_query(body: AgentQueryIn, request: Request):
    """상위 agent(예: 통합 VOC)가 AI-Infra 질문을 위임하는 엔드포인트(인증 없음, 내부망 전용).
    단일 user_id로 장기 메모리를 로드/저장하며, 이후 대화에서 참고한다."""
    if not (body.user_id or "").strip() or not (body.message or "").strip():
        raise HTTPException(400, "user_id와 message는 필수입니다.")
    user_id = _to_os_identity(body.user_id)[:128] or "anonymous"
    roles = ",".join([r.strip() for r in (body.roles or []) if r and r.strip()])
    request_id = f"agentq-{uuid.uuid4().hex[:12]}"
    model_name = await _display_model_name()

    mem_enabled = body.use_memory and _mem_on(await get_config("memory_enabled", "true"))
    show_tools = _mem_on(await get_config("show_tool_activity", "true"))
    # conversation_id가 없으면 시간(일 단위)으로 자동 부여 -> 같은 날 같은 사용자는 이어짐.
    conv = (body.conversation_id or "").strip() or (_auto_conv(user_id) if mem_enabled else None)
    history, extra_instruction = ([], None)
    if mem_enabled:
        history, extra_instruction = await _memory_context(user_id, conv, body.message)
    rag_block, manual_hits, voc_hits = await _rag_context(body.message, history)
    if rag_block:
        extra_instruction = (extra_instruction or "") + rag_block

    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=body.message)])
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv or session_id,
        "X-Request-Id": request_id,
        "X-User-Roles": roles,
    }
    raw = await _make_raw_outputs()
    ground = await _make_grounding(body.message, user_id)
    ground.seed_rag(manual_hits, voc_hits)
    prepared: dict = {"toolsets": []}

    async def _prepare():
        """MCP toolset은 **만든 태스크에서 닫아야 한다** (#164). 스트리밍 응답의 제너레이터는
        엔드포인트 코루틴과 **다른 태스크**에서 돌기 때문에, 밖에서 만들고 안에서 닫으면
        anyio가 `Attempted to exit a cancel scope ...`로 요청을 통째로 죽인다."""
        agent, _model, toolsets = await build_agent(caller_headers, extra_instruction)
        prepared["toolsets"] = toolsets
        return Runner(agent=agent, app_name=APP_NAME,
                      session_service=state["session_service"])

    if not body.stream:
        runner = await _prepare()
        final_text = ""
        try:
            with _trace_ctx(user_id, conv, body.source or "agent-api"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
        _bg_persist(user_id, conv, body.source, body.message, final_text, mem_enabled)
        answer = ground.review(final_text)
        if raw:
            answer += raw.block() + await _raw_output_summary(raw, body.message)
        return JSONResponse({"answer": answer, "conversation_id": conv,
                             "request_id": request_id})

    async def event_stream():
        dedup = _StreamDedup()
        charts = _chart_inliner()
        in_think = False
        try:
            runner = await _prepare()
            with _trace_ctx(user_id, conv, body.source or "agent-api"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message,
                                                    run_config=STREAMING_RUN_CONFIG):
                    if await request.is_disconnected():
                        break
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    if show_tools:
                        status = _tool_status_lines(event)
                        if status:
                            in_think = True
                            yield _sse(request_id, model_name, status + "\n")
                    delta = dedup.feed(event)
                    if delta:
                        if ground.hold:
                            continue          # 검사 후 한 번에 내보낸다(#155)
                        # 차트 표시자가 델타 경계에 걸쳐 쪼개져 올 수 있어 안전한 부분만 흘린다.
                        out = await charts.feed(delta)
                        if out:
                            if in_think:      # 진행 줄과 답변 사이만 한 줄 띄운다
                                yield _sse(request_id, model_name, "\n")
                                in_think = False
                            yield _sse(request_id, model_name, out)
            tail = (await charts.whole(ground.review(dedup.full)) if ground.hold
                    else await charts.flush())    # 붙들고 있던 꼬리 마무리
            if tail:
                if in_think:
                    yield _sse(request_id, model_name, "\n")
                    in_think = False
                yield _sse(request_id, model_name, tail)
            if in_think:
                yield _sse(request_id, model_name, "\n")
            if raw:
                block = raw.block()
                if block:
                    yield _sse(request_id, model_name, block)
                    summary = await _raw_output_summary(raw, body.message)
                    if summary:
                        yield _sse(request_id, model_name, summary)
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            yield _sse(request_id, model_name, f"\n\n[{await _friendly_error(e)}]")
            yield _sse(request_id, model_name, "", finish=True)
            yield "data: [DONE]\n\n"
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
            _bg_persist(user_id, conv, body.source, body.message, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")


# --- 장기 메모리 관리 ---
# **여기도 인증이 있어야 한다**(#143). 예전 주석은 "인증 없음, 내부망 전용"이었는데, 8500 포트는
# 0.0.0.0에 열려 있다(외부 VOC agent가 붙어야 해서 닫을 수 없다). 그래서 '내부망 전용'이라는
# 전제가 성립하지 않는다 - #139에서 `/v1/*` 네 개에 인증을 걸면서 이 셋을 빠뜨렸다.
#
# 셋 중 POST가 특히 위험하다. 여기 넣은 내용은 그 사용자의 **다음 대화부터 시스템 지시문에
# 붙는다**(`_memory_context` -> `extra_instruction`). 즉 남의 에이전트에 영구적인 지시를
# 심을 수 있다. GET은 남의 대화에서 증류된 내용을 읽고, DELETE는 통째로 지운다.
class MemoryAddIn(BaseModel):
    content: str
    kind: str = "fact"


@app.get("/v1/memory/{user_id}", dependencies=[Depends(require_api_key)])
async def memory_list(user_id: str):
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    return {"user_id": uid, "items": await list_user_memory(uid)}


@app.post("/v1/memory/{user_id}", dependencies=[Depends(require_api_key)])
async def memory_add(user_id: str, body: MemoryAddIn):
    if not (body.content or "").strip():
        raise HTTPException(400, "content는 필수입니다.")
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    mid = await add_user_memory(uid, body.content.strip(), body.kind or "fact", source="manual")
    return {"id": mid}


@app.delete("/v1/memory/{user_id}", dependencies=[Depends(require_api_key)])
async def memory_delete(user_id: str, memory_id: int | None = None):
    """memory_id 쿼리로 개별 삭제, 없으면 사용자 기억 전체 삭제(잊힐 권리)."""
    uid = _to_os_identity(user_id)[:128] or "anonymous"
    deleted = await delete_user_memory(uid, memory_id)
    return {"deleted": deleted}


# ================================================================= 통합 VOC agent 연동 (guide 계약)
# 입력: voc_info + output_option / 출력: {success, answer:{content, similar_voc?}, evaluation?}
# 필수: success, (성공 시) answer.content. similar_voc/evaluation은 선택(service hub mcp 연동은 추후).
class _VocRef(BaseModel):
    id: str | None = None
    name: str | None = None


class _VocRequester(BaseModel):
    user_id: str | None = None
    user_name: str | None = None
    user_dept: str | None = None


class _VocContent(BaseModel):
    text: str | None = None
    raw_text: str | None = None


class VocInfo(BaseModel):
    voc_id: str | None = None
    voc_title: str | None = None
    voc_status: str | None = None
    voc_status_name: str | None = None
    voc_class_code: str | None = None
    voc_class_name: str | None = None
    system: _VocRef | None = None
    sub_system: _VocRef | None = None
    division: _VocRef | None = None
    campus: _VocRef | None = None
    line: _VocRef | None = None
    requester: _VocRequester | None = None
    created_at: str | None = None
    voc_content: _VocContent | None = None


class VocQueryIn(BaseModel):
    voc_info: VocInfo
    output_option: str = "markdown"   # "markdown" | "html"
    stream: bool = False              # 확장: SSE 스트리밍(가이드 기본은 비스트림 JSON)
    use_memory: bool = True


_TAG_RE = re.compile(r"<[^>]+>")


def _voc_body_text(v: VocInfo) -> str:
    """VOC 본문을 뽑는다. text 우선, 없으면 raw_text의 태그를 제거해 사용."""
    c = v.voc_content
    if not c:
        return ""
    body = (c.text or "").strip()
    if not body and c.raw_text:
        body = re.sub(r"\s+", " ", _TAG_RE.sub(" ", c.raw_text)).strip()
    return body


def _voc_message(v: VocInfo, body: str) -> str:
    parts = []
    if v.voc_title:
        parts.append(f"[VOC 제목] {v.voc_title}")
    sysname = v.system.name if v.system else None
    subname = v.sub_system.name if v.sub_system else None
    if sysname or subname:
        parts.append(f"[시스템] {sysname or '-'} / {subname or '-'}")
    if v.voc_class_name:
        parts.append(f"[분류] {v.voc_class_name}")
    if v.requester and v.requester.user_dept:
        parts.append(f"[요청 부서] {v.requester.user_dept}")
    parts.append(f"[문의 내용]\n{body}")
    return "\n".join(parts)


async def _voc_similar(v: VocInfo, query: str) -> list:
    """Service Hub MCP로 유사 VOC를 조회한다(설정/방화벽 없으면 빈 리스트).
    현재 VOC의 시스템명으로 필터해 관련도를 높인다."""
    try:
        k = int(await get_config("voc_similar_top_k", "3"))
    except (TypeError, ValueError):
        k = 3
    if k <= 0:
        return []
    system_name = v.system.name if v.system else None
    return await search_similar_voc(query, system_name, k)


def _voc_format_instruction(output_option: str) -> str:
    if (output_option or "").lower() == "html":
        return ("\n\n## 출력 형식(반드시 준수)\n답변 전체를 유효한 HTML 조각으로만 출력한다. "
                "마크다운/코드펜스(```)를 쓰지 말고, 제목은 <h2>/<h3>, 목록은 <ul><li>, "
                "표는 <table><tr><td>로 구조화하며 여는/닫는 태그를 정확히 맞춘다.")
    return ("\n\n## 출력 형식(반드시 준수)\n답변 전체를 마크다운으로만 출력한다. "
            "제목/목록/표/코드블록을 적절히 사용한다.")


@app.post("/v1/voc/query", dependencies=[Depends(require_api_key)])
async def voc_query(body: VocQueryIn, request: Request):
    """통합 VOC agent가 AI-Infra 관련 VOC를 위임하는 엔드포인트(내부망 전용, 인증 없음).
    guide 계약대로 voc_info를 받아 분석 답변을 {success, answer:{content}} 형태로 돌려준다.
    output_option(markdown|html)에 맞춰 답변 형식을 강제하고, requester.user_id로 장기 메모리를 공유한다."""
    v = body.voc_info
    user_id = _to_os_identity((v.requester.user_id if v.requester else None) or "")[:128] or "anonymous"
    body_text = _voc_body_text(v)
    if not body_text:
        return JSONResponse({"success": False, "answer": None,
                             "error": "voc_content(text/raw_text)가 비어 있습니다."}, status_code=400)

    message = _voc_message(v, body_text)
    request_id = f"voc-{uuid.uuid4().hex[:12]}"
    conv = (v.voc_id or "").strip() or _auto_conv(user_id)   # VOC 단위로 대화 스레드
    mem_enabled = body.use_memory and _mem_on(await get_config("memory_enabled", "true"))

    history, extra_instruction = ([], None)
    if mem_enabled:
        history, extra_instruction = await _memory_context(user_id, conv, message)
    fmt = _voc_format_instruction(body.output_option)
    extra_instruction = (extra_instruction + fmt) if extra_instruction else fmt
    rag_block, manual_hits, voc_hits = await _rag_context(body_text, history)
    extra_instruction += rag_block

    session_id = await _create_session(user_id, history)
    new_message = types.Content(role="user", parts=[types.Part(text=message)])
    caller_headers = {
        "X-User-Id": user_id,
        "X-Conversation-Id": conv,
        "X-Request-Id": request_id,
        "X-User-Roles": "",
    }
    # 유사 VOC 조회는 에이전트 응답과 병렬로 돌린다(지연 최소화). Service Hub 미설정 시 빈 리스트.
    similar_task = asyncio.create_task(_voc_similar(v, body_text))

    async def _collect_similar():
        try:
            return await similar_task
        except Exception:  # noqa: BLE001
            return []

    raw = await _make_raw_outputs()
    ground = await _make_grounding(body_text, user_id)
    ground.seed_rag(manual_hits, voc_hits)
    prepared: dict = {"toolsets": []}

    async def _prepare():
        """MCP toolset은 **만든 태스크에서 닫아야 한다** (#164). 자세한 이유는 agent_query 참고."""
        agent, _model, toolsets = await build_agent(caller_headers, extra_instruction)
        prepared["toolsets"] = toolsets
        return Runner(agent=agent, app_name=APP_NAME,
                      session_service=state["session_service"])

    if not body.stream:
        runner = await _prepare()
        final_text, ok = "", True
        try:
            with _trace_ctx(user_id, conv, "voc-agent"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message):
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    if event.is_final_response():
                        final_text = _event_text(event) or final_text
        except Exception as e:  # noqa: BLE001
            print(f"[agent] voc_query 오류: {e}")
            ok = False
        finally:
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
        similar = await _collect_similar()
        _bg_persist(user_id, conv, "voc-agent", message, final_text, mem_enabled)
        if not ok or not final_text:
            return JSONResponse({"success": False, "answer": None})
        # 외부로 나가는 본문에서는 차트 표시자를 실제 이미지로 바꿔 준다(이력은 표시자 유지).
        content = await _chart_inliner().whole(ground.review(final_text))
        if raw:
            content += raw.block() + await _raw_output_summary(raw, body_text)
        answer = {"content": content}
        if similar:
            answer["similar_voc"] = similar
        return JSONResponse({"success": True, "answer": answer})

    async def event_stream():
        dedup = _StreamDedup()
        in_think = False
        try:
            runner = await _prepare()
            with _trace_ctx(user_id, conv, "voc-agent"):
                async for event in runner.run_async(user_id=user_id, session_id=session_id,
                                                    new_message=new_message,
                                                    run_config=STREAMING_RUN_CONFIG):
                    if await request.is_disconnected():
                        break
                    if raw:
                        raw.observe(event)
                    ground.observe(event)
                    delta = dedup.feed(event)
                    # 검사가 켜져 있으면 델타를 흘리지 않는다 — 아래 envelope이 검사를 통과한
                    # 본문을 통째로 싣는다(#155). 델타를 먼저 보내면 되돌릴 수 없다.
                    if delta and not ground.hold:
                        yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
            # 마지막에 가이드 계약 형태의 완성 envelope을 한 번 더 보낸다.
            if dedup.full:
                similar = await _collect_similar()
                content = await _chart_inliner().whole(ground.review(dedup.full))
                if raw:
                    content += raw.block() + await _raw_output_summary(raw, body_text)
                answer = {"content": content}
                if similar:
                    answer["similar_voc"] = similar
                envelope = {"success": True, "answer": answer}
            else:
                envelope = {"success": False, "answer": None}
            yield f"data: {json.dumps(envelope, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'success': False, 'answer': None, 'error': str(e)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"
        finally:
            if not similar_task.done():   # 답이 비어 await를 안 한 경우 고아 방지
                similar_task.cancel()
            await _cleanup_session(user_id, session_id)
            await _close_toolsets(prepared["toolsets"])
            _bg_persist(user_id, conv, "voc-agent", message, dedup.full, mem_enabled)

    return StreamingResponse(event_stream(), media_type="text/event-stream")
