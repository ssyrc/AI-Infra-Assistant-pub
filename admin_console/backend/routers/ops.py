"""
관리자 콘솔에서 특정 서비스 컨테이너를 재시작하는 기능(설정 저장/화이트리스트 변경 후 바로
반영하기 위함). Docker 소켓(/var/run/docker.sock)을 admin-console에 마운트해야 동작한다.

⚠️ 이 소켓 마운트는 admin-console 컨테이너에게 사실상 호스트 수준 권한을 준다(임의 컨테이너
실행/마운트가 가능해짐). 그 위험을 줄이려고 이 API 자체는 "정해진 서비스 이름 재시작"만
허용하고 임의 명령 실행 경로는 없다 — 그래도 admin-console 계정이 뚫리면 파급력이 커진다는
점은 그대로이니, 접근을 신뢰된 관리자망으로 제한하는 게 중요하다.
"""
import docker
import httpx
from fastapi import APIRouter, Depends, HTTPException

from auth import require_admin
from config_store import get_config
from db import get_http_client

router = APIRouter(prefix="/api/ops", tags=["ops"])

ALLOWED_SERVICES = {"agent-server", "manual-mcp", "execution-mcp", "voc-mcp", "chart-mcp"}
AGENT_SERVER_URL = "http://agent-server:8000"


@router.post("/restart/{service}")
async def restart_service(service: str, admin: str = Depends(require_admin)):
    if service not in ALLOWED_SERVICES:
        raise HTTPException(400, f"재시작 가능한 서비스가 아닙니다: {service}")
    try:
        client = docker.from_env()
        matches = client.containers.list(all=True, filters={"name": service})
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            500,
            f"docker 소켓에 접근할 수 없습니다({type(e).__name__}: {e}). "
            "docker-compose에 /var/run/docker.sock 마운트가 필요합니다.",
        )
    if not matches:
        raise HTTPException(404, f"'{service}' 컨테이너를 찾을 수 없습니다.")
    for c in matches:
        c.restart(timeout=15)
    return {"ok": True, "restarted": [c.name for c in matches]}


@router.post("/sync-openwebui-model")
async def sync_openwebui_model(admin: str = Depends(require_admin)):
    """agent-server가 지금 노출하는 모델명(mock이면 실제 모델명, 실제 백엔드면 "AI Infra
    Assistant")을 Open WebUI의 기본 모델로 지정한다. Open WebUI는 자체 로그인/권한 체계가
    있어서 관리자 API 키가 필요하다(Open WebUI 로그인 -> 설정 -> 계정 -> API 키 발급 후
    설정 탭의 openwebui_admin_api_key에 저장)."""
    base_url = (await get_config("openwebui_base_url", "")).rstrip("/")
    api_key = await get_config("openwebui_admin_api_key", "")
    if not base_url or not api_key:
        raise HTTPException(
            400,
            "openwebui_base_url / openwebui_admin_api_key 설정이 비어 있습니다. Open WebUI에 "
            "관리자로 로그인 -> 설정 -> 계정 -> API 키 발급 후 설정 탭에 저장하세요.",
        )

    client = await get_http_client()
    # **agent-server의 /v1/*에는 인증이 걸려 있다**(#139의 `agent_api_key`). 콘솔도 예외가
    # 아니라서 키를 함께 보내야 한다 - 예전에는 헤더 없이 불렀고, 그래서 `agent_api_key`를
    # 넣는 순간 이 버튼이 값이 맞든 틀리든 401로 죽었다(#142).
    agent_key = (await get_config("agent_api_key", "") or "").strip()
    agent_headers = {"Authorization": f"Bearer {agent_key}"} if agent_key else {}
    try:
        models_resp = await client.get(f"{AGENT_SERVER_URL}/v1/models", headers=agent_headers)
        models_resp.raise_for_status()
        models = models_resp.json().get("data") or []
        if not models:
            raise HTTPException(502, "agent-server가 노출하는 모델이 없습니다.")
        model_id = models[0]["id"]
    except httpx.HTTPStatusError as e:
        # 401이면 원인이 좁다. 콘솔과 agent-server는 **같은 DB의 같은 키**를 읽으므로,
        # 값이 다를 수 있는 경우는 설정 캐시(5초) 창뿐이다. 재시작은 필요 없다
        # (`agent_api_key`는 hot_reload=true라 매 요청 새로 읽는다).
        if e.response.status_code == 401:
            raise HTTPException(
                502,
                "agent-server가 401을 돌려줬습니다. `agent_api_key`를 방금 바꿨다면 "
                "설정 캐시(5초) 때문일 수 있으니 잠시 뒤 다시 저장해 보세요. "
                "계속 401이면 agent-server가 콘솔과 다른 DB를 보고 있는 것입니다"
                + ("(콘솔에는 키가 설정돼 있습니다)." if agent_key
                   else "(콘솔의 agent_api_key는 비어 있는데 agent-server는 키를 "
                        "요구하고 있습니다)."))
        raise HTTPException(502, f"agent-server /v1/models 조회 실패: {e}")
    except httpx.HTTPError as e:
        raise HTTPException(502, f"agent-server /v1/models 조회 실패: {e}")

    try:
        resp = await client.post(
            f"{base_url}/api/v1/configs/models",
            json={"DEFAULT_MODELS": model_id, "MODEL_ORDER_LIST": [model_id]},
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as e:
        raise HTTPException(
            502,
            f"Open WebUI 설정 실패({e.response.status_code}): API 키가 관리자 권한인지 확인하세요. {e.response.text}",
        )
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Open WebUI에 연결할 수 없습니다: {e}")

    return {"ok": True, "default_model": model_id}
