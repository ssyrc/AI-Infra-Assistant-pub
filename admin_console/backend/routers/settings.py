"""
플랫폼 전역 설정(vLLM 주소, 각 MCP DB DSN, MCP 엔드포인트, 에이전트 시스템 지시문) 관리 API.
실제 값은 shared/config_store.py를 통해 platform_config DB의 platform_settings 테이블에 저장된다.
"""
import os
import sys

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import require_admin

sys.path.append(os.path.join(os.path.dirname(__file__), "../../../shared"))
from config_store import list_config, set_config  # noqa: E402

router = APIRouter(prefix="/api/settings", tags=["settings"])

# .env(환경변수)에서 매 기동 시 재주입되는(force=True) 키들.
# 콘솔에서 고쳐도 재시작하면 .env 값으로 덮어써지므로, UI에서 읽기 전용으로 표시하고
# 저장 요청도 막는다(무의미한 편집로 인한 혼란 방지). 값 변경은 .env에서 한다.
# (shared/migrations.py::config_seed의 force=True 항목과 일치해야 한다.)
ENV_MANAGED_KEYS = {
    "manual_db_dsn", "voc_db_dsn", "execution_db_dsn", "system_db_dsn",
    "agent_session_db_dsn", "memory_db_dsn", "redis_url",
}


class SettingIn(BaseModel):
    value: str


@router.get("")
async def get_settings(admin: str = Depends(require_admin)):
    rows = await list_config()
    for r in rows:
        r["env_managed"] = r["key"] in ENV_MANAGED_KEYS
        if r["is_secret"] and r["value"]:
            r["value"] = "•" * 8 + r["value"][-4:] if len(r["value"]) > 4 else "••••"
    return rows


# 이 키를 저장하면 Open WebUI 기본 모델 지정까지 자동으로 이어서 한다.
# 예전에는 저장 후 별도 버튼을 눌러야 했는데, agent나 DB를 재기동할 때마다 반복해야 해서
# 번거롭다는 요청이 있었다. 저장 하나로 끝낸다.
_AUTO_SYNC_OPENWEBUI = {"openwebui_admin_api_key", "openwebui_base_url"}


@router.put("/{key}")
async def update_setting(key: str, body: SettingIn, admin: str = Depends(require_admin)):
    if key in ENV_MANAGED_KEYS:
        raise HTTPException(
            400, "이 값은 .env(환경변수)로 관리됩니다. .env를 수정하고 해당 서비스를 재시작하세요.")
    await set_config(key, body.value, updated_by=admin)

    if key in _AUTO_SYNC_OPENWEBUI and body.value.strip():
        # 실패해도 저장 자체는 성공으로 둔다(키를 막 넣은 직후라 Open WebUI가 아직 준비 전일 수
        # 있다). 결과만 알려주고, 필요하면 사용자가 다시 저장하면 된다.
        from routers.ops import sync_openwebui_model
        try:
            synced = await sync_openwebui_model(admin=admin)
            return {"ok": True, "openwebui_default_model": synced.get("default_model")}
        except HTTPException as e:
            return {"ok": True, "openwebui_sync_error": str(e.detail)}
        except Exception as e:  # noqa: BLE001
            return {"ok": True, "openwebui_sync_error": f"{type(e).__name__}: {e}"}
    return {"ok": True}


def _read_instruction_from_disk() -> str:
    """`shared/agent_instruction.py`에서 지시문을 **지금 이 순간의 파일 내용으로** 읽는다 (#147).

    왜 import를 쓰지 않나 — 두 겹의 캐시가 있다.
      1) `sys.modules`: 함수 안에서 `from agent_instruction import ...` 해도 이미 읽은 모듈이면
         그대로 돌려준다. 그래서 버튼을 두 번째 누르는 순간부터는 **옛 텍스트가 저장됐다.**
         `./shared`가 바인드 마운트라 파일은 최신인데 버튼이 아무 일도 안 하는 상태였다.
      2) `.pyc`: `importlib.reload`로 1)을 피해도, 바이트코드 캐시는 **mtime+크기**로 유효성을
         판단한다. 같은 초에 같은 크기로 바뀌면 낡은 바이트코드를 그대로 쓴다(실제로 재현했다).

    그래서 소스를 직접 읽어 `compile`한다. 우리가 읽은 문자열을 컴파일하는 경로는 pyc 캐시를
    타지 않으므로 항상 최신이다. `agent_instruction.py`는 docstring과 문자열 상수뿐이라
    (부수효과 없음) exec해도 안전하다.
    """
    path = os.path.join(os.path.dirname(__file__), "../../../shared/agent_instruction.py")
    path = os.path.normpath(path)
    try:
        src = open(path, encoding="utf-8").read()
        ns: dict = {}
        exec(compile(src, path, "exec"), ns)          # noqa: S102 - 우리 저장소의 상수 파일
        text = ns["AGENT_INSTRUCTION"]
    except Exception as e:                            # noqa: BLE001
        # 못 읽었으면 **조용히 옛 값을 쓰지 않는다** - 그게 이 버그의 본질이었다.
        raise HTTPException(
            500, f"지시문 파일을 읽지 못했습니다({type(e).__name__}: {e}). 경로: {path}")
    if not isinstance(text, str) or not text.strip():
        raise HTTPException(500, "지시문 파일에서 읽은 값이 비어 있습니다.")
    return text


@router.post("/agent_system_instruction/reset")
async def reset_agent_instruction(admin: str = Depends(require_admin)):
    """지시문을 **코드의 현재 기본값**으로 되돌린다.

    왜 필요한가: `agent_system_instruction`은 non-force 시드라 db-init을 다시 돌려도
    기존 DB 값을 덮지 않는다(관리자가 손댄 문구를 지우면 안 되므로). 그래서 지시문을 고칠
    때마다 1만 자짜리 전문을 문서에 붙이고 사람이 복사·붙여넣기 해 왔다 - 매번 반복이고
    중간에 잘리면 조용히 깨진다. 버튼 하나로 끝나게 한다.

    직접 수정한 문구가 있으면 사라지므로, 프런트에서 확인창을 띄운다.
    """
    text = _read_instruction_from_disk()

    await set_config("agent_system_instruction", text, updated_by=admin)
    # hot_reload=false 키라 agent-server를 재시작해야 반영된다(프런트가 버튼을 띄운다).
    return {"ok": True, "chars": len(text), "restart_required": True}
