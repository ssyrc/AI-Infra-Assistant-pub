"""
중앙 설정 저장소 (platform_config DB의 platform_settings 테이블).

모든 서비스(agent_server, 4개 MCP, admin_console)가 이 모듈을 통해서만
vLLM 주소, 각 MCP의 DB DSN, MCP 엔드포인트 URL 등을 읽는다.
-> IP/포트를 바꿀 때 코드를 여러 곳 고칠 필요 없이 이 테이블 하나만 바꾸면 된다.
-> 관리자 콘솔의 '설정' 탭이 이 테이블을 CRUD한다.

hot_reload=true인 키(vLLM 주소 등, 매 호출마다 새로 연결하는 값)는 캐시 TTL만 지나면
재시작 없이 바로 반영된다. hot_reload=false인 키(DB DSN, MCP 엔드포인트 URL처럼
서비스 시작 시 커넥션/연결을 맺어두는 값)는 해당 컨테이너를 재시작해야 반영된다
(관리자 콘솔에 이 안내가 표시된다).
"""
import os
import time
import asyncpg

CONFIG_DB_DSN = os.environ["CONFIG_DB_DSN"]

_pool: asyncpg.Pool | None = None
_cache: dict[str, str] = {}
_cache_ts: float = 0.0
CACHE_TTL_SECONDS = 5


async def _get_config_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(CONFIG_DB_DSN, min_size=1, max_size=5)
    return _pool


async def _refresh_cache_if_stale():
    global _cache, _cache_ts
    if time.time() - _cache_ts < CACHE_TTL_SECONDS and _cache:
        return
    pool = await _get_config_pool()
    rows = await pool.fetch("SELECT key, value FROM platform_settings")
    _cache = {r["key"]: r["value"] for r in rows}
    _cache_ts = time.time()


async def get_config(key: str, default: str | None = None) -> str | None:
    await _refresh_cache_if_stale()
    return _cache.get(key, default)


async def set_config(key: str, value: str, updated_by: str | None = None):
    pool = await _get_config_pool()
    await pool.execute(
        """
        INSERT INTO platform_settings (key, value, updated_by, updated_at)
        VALUES ($1, $2, $3, now())
        ON CONFLICT (key) DO UPDATE SET value = $2, updated_by = $3, updated_at = now()
        """,
        key,
        value,
        updated_by,
    )
    global _cache_ts
    _cache_ts = 0  # 다음 조회 때 즉시 갱신


async def list_config() -> list[dict]:
    pool = await _get_config_pool()
    rows = await pool.fetch(
        "SELECT key, value, description, hot_reload, is_secret, updated_by, updated_at "
        "FROM platform_settings ORDER BY key"
    )
    return [dict(r) for r in rows]
