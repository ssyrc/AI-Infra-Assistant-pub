"""
ADK 루트 에이전트 빌더.
- LLM/MCP 엔드포인트/시스템 지시문을 전부 config_store(platform_settings)에서 읽는다.
- MCP 호출 시 호출자 식별 헤더(X-User-Id 등)를 함께 보내 Execution MCP 감사로그에 남긴다.
- Tracing: Langfuse (키가 없으면 자동 비활성화).
"""
import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "../shared"))

# --- Langfuse 트레이싱: 앱 임포트 전에 가장 먼저 초기화 ---------------------
if os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY"):
    try:
        from openinference.instrumentation.google_adk import GoogleADKInstrumentor
        from langfuse import Langfuse

        Langfuse(
            public_key=os.environ["LANGFUSE_PUBLIC_KEY"],
            secret_key=os.environ["LANGFUSE_SECRET_KEY"],
            host=os.environ.get("LANGFUSE_HOST", "http://langfuse-web:3000"),
        )
        GoogleADKInstrumentor().instrument()
    except Exception as e:  # noqa: BLE001
        print(f"[agent] Langfuse 트레이싱 초기화 실패, 트레이싱 없이 계속 진행: {e}")
else:
    print("[agent] LANGFUSE 키가 없어 트레이싱을 비활성화합니다.")
# --------------------------------------------------------------------------

from google.adk.agents import Agent
from google.genai import types
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StreamableHTTPConnectionParams

from config_store import get_config

APP_NAME = "ops_assistant"

DEFAULT_INSTRUCTION = """당신은 사내 시스템 운영/사용을 돕는 한국어 어시스턴트입니다.
검색 결과에 근거해서만 답하고, 출처를 함께 제시하세요."""


async def build_agent(caller_headers: dict | None = None,
                      extra_instruction: str | None = None) -> tuple[Agent, str, list[McpToolset]]:
    """config_store의 현재 설정값으로 ADK 에이전트를 만든다.

    caller_headers가 주어지면 MCP 호출에 호출자 식별 헤더(X-User-Id 등)를 함께 보낸다.
    이 헤더는 요청마다 달라지므로(사용자별), 에이전트를 요청 단위로 만든다. Execution MCP는
    이 헤더로 user_scoped 툴의 user_id를 강제 주입하고 감사로그·권한검사를 수행한다.

    반환하는 toolset 목록은 요청 종료 시 호출자가 close()로 정리한다."""
    llm_base_url = await get_config("vllm_llm_base_url")
    llm_model = await get_config("vllm_llm_model", "qwen3-32b")
    # 온도가 높으면 조회 결과 대신 학습 지식으로 그럴듯한 절차를 지어내기 쉽다(사내 시스템에
    # 없는 스케줄러 문법을 답하는 등). 근거 충실도를 위해 기본을 낮게 둔다.
    try:
        temperature = float(await get_config("llm_temperature", "0.2"))
    except (TypeError, ValueError):
        temperature = 0.2
    instruction = await get_config("agent_system_instruction", DEFAULT_INSTRUCTION)
    # 로그인 서버 주소는 설정 탭에서 바뀔 수 있으므로 지시문에 하드코딩하지 않고 매 요청 주입한다
    # (Execution MCP의 "로그인 서버 실행" 툴은 host를 자동 고정하지만, disk_free처럼 host가
    # 노출된 툴에서 에이전트가 로그인 서버를 직접 지정해야 하는 경우를 위함).
    # 이름이 아니라 **IP**다 - 이름 해석(/etc/hosts)이 엉뚱한 서버를 가리킨 사고가 있었다.
    login_host = await get_config("execution_host", "10.0.0.100")
    # 운영팀 접수 경로도 설정에서 읽어 붙인다(포탈 메뉴가 바뀌어도 지시문을 고칠 필요 없음).
    voc_intake = (await get_config("voc_intake_guide", "") or "").strip()
    # 환경 값은 **구조화된 블록**으로 붙인다. 예전에는 지시문 끝에
    #   `(참고: 커맨드를 실행할 로그인 서버 주소는 '...'입니다.)`
    # 처럼 괄호 문장으로 붙였는데, 모델이 그 꼴을 '답변 꼬리말 서식'으로 보고 **답변에 그대로
    # 베껴 썼다**. 심지어 같은 모양으로 `(참고: GPU_서버_활용_가이드_(KOR))`을 새로 만들어
    # 붙이기까지 했다(#125). 값은 라벨로 주고, 이 블록을 옮겨 쓰지 말라고 못박는다.
    # **질문한 사람의 계정을 알려 준다.** 이게 없으면 모델은 자기 이름(`ops_assistant` —
    # ADK가 시스템 프롬프트에 넣는 에이전트 이름)을 사용자 계정으로 착각한다. 실제로
    # "현재 로그인한 사용자(ops_assistant)의 job만 조회할 수 있습니다"라고 답했다(#140).
    # 커맨드에 넣을 값이 아니라 **답변에서 사용자를 부를 때** 쓰는 값이다(실행 계정은
    # 헤더에서 MCP가 강제 주입한다).
    caller_id = (caller_headers or {}).get("X-User-Id") or ""
    env_lines = []
    if caller_id:
        env_lines.append(f"질문한 사용자 계정: {caller_id}")
    env_lines.append(f"로그인 서버 주소: {login_host}")
    if voc_intake:
        env_lines.append(f"운영팀 접수 경로: {voc_intake}")
    instruction = (
        f"{instruction}\n\n# 이 환경의 값\n"
        "아래는 도구 호출과 안내에 쓰는 값입니다. **이 블록을 답변에 옮겨 적지 마세요.**\n"
        "괄호로 감싼 '(참고: …)' 같은 꼬리말을 답변에 만들지 마세요.\n"
        + "\n".join(f"- {line}" for line in env_lines)
    )
    if extra_instruction:
        # 요청별 컨텍스트(예: 사용자 장기 메모리)를 시스템 지시문 뒤에 덧붙인다.
        instruction = f"{instruction}\n{extra_instruction}"

    # ADK는 **문자열** instruction 안의 `{...}`를 세션 상태 변수로 보고 치환을 시도한다.
    # 우리 지시문에는 개인정보 자리표시자(`{사업부명}` `{팀명}` 등)가 들어 있고, 한글은
    # 파이썬 식별자로 인정되므로(`'사업부명'.isidentifier() == True`) 상태에 없는 변수로
    # 판정돼 첫 요청부터 `Context variable not found: 사업부명.`으로 죽는다.
    # (`{사용자 id}`처럼 공백이 든 것은 식별자가 아니라 그냥 통과했다 — 그래서 단어 하나짜리
    #  자리표시자만 터졌다.)
    # instruction을 콜러블(InstructionProvider)로 넘기면 ADK가 치환 단계를 통째로 건너뛴다
    # (llm_agent.canonical_instruction의 bypass_state_injection=True).
    # 장기 메모리로 붙는 사용자 대화 내용에 중괄호가 섞여도 같은 이유로 안전해진다.
    def instruction_provider(_ctx=None, _text=instruction) -> str:
        return _text

    urls = {
        "manual": await get_config("manual_mcp_url"),
        # 커맨드 실행은 Execution MCP 하나로 통합됐다(구 Command MCP + System MCP, #111).
        "execution": await get_config("execution_mcp_url"),
        "voc": await get_config("voc_mcp_url"),
    }
    missing = [k for k, v in urls.items() if not v]
    if missing:
        raise RuntimeError(f"MCP 주소가 설정되지 않았습니다: {', '.join(missing)}")

    # 차트는 부가 기능이라 **없어도 서비스가 떠야 한다**. 주소가 비어 있으면 조용히 건너뛴다
    # (아직 배포하지 않은 환경, 또는 차트를 쓰지 않기로 한 환경).
    chart_url = (await get_config("chart_mcp_url", "") or "").strip()

    headers = {k: v for k, v in (caller_headers or {}).items() if v is not None}
    # MCP는 X-User-Id를 그대로 믿고 그 계정 권한으로 커맨드를 실행한다. MCP 포트가 호스트에
    # 열려 있으면 같은 망의 누구나 그 헤더를 붙여 **남의 계정으로 실행**할 수 있으므로,
    # agent-server만 아는 공유 비밀값을 함께 보낸다(db-init이 무작위로 한 번 심는다).
    mcp_secret = (await get_config("mcp_shared_secret", "") or "").strip()
    if mcp_secret:
        headers["X-Agent-Secret"] = mcp_secret

    def toolset(url: str) -> McpToolset:
        return McpToolset(
            connection_params=StreamableHTTPConnectionParams(url=url, headers=headers or None))

    toolsets = [toolset(urls["manual"]), toolset(urls["execution"]), toolset(urls["voc"])]
    if chart_url:
        toolsets.append(toolset(chart_url))
    agent = Agent(
        model=LiteLlm(model=f"openai/{llm_model}", api_base=llm_base_url, api_key="not-needed"),
        name="ops_assistant",
        instruction=instruction_provider,
        generate_content_config=types.GenerateContentConfig(temperature=temperature),
        tools=list(toolsets),
    )
    return agent, llm_model, toolsets
