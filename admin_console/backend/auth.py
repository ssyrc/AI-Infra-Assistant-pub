"""
운영자 콘솔용 최소 인증.
사내 SSO 연동 전까지는 HTTP Basic 인증으로 막고, nginx 등 리버스 프록시로
접근 자체를 관리망/VPN 대역으로 제한하는 것을 전제로 한다.

.env의 ADMIN_USER/ADMIN_PASSWORD는 잠금 방지용 기본 계정으로 항상 유효하다(계정 관리
탭에서 지울 수 없음). 그 외 관리자는 admin_accounts(platform_config DB)에 등록하며,
비밀번호는 bcrypt 해시로만 저장한다.
"""
import os
import secrets

import asyncpg
import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic()

ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
CONFIG_DB_DSN = os.environ["CONFIG_DB_DSN"]

_pool: asyncpg.Pool | None = None


async def get_config_pool() -> asyncpg.Pool:
    """platform_config DB(admin_accounts가 있는 곳) 커넥션 풀. 계정 관리 라우터도 공유해서 쓴다."""
    global _pool
    if _pool is None:
        _pool = await asyncpg.create_pool(CONFIG_DB_DSN, min_size=1, max_size=5)
    return _pool


async def _check_db_account(username: str, password: str) -> bool:
    pool = await get_config_pool()
    row = await pool.fetchrow(
        "SELECT password_hash FROM admin_accounts WHERE username = $1", username)
    if not row:
        return False
    return bcrypt.checkpw(password.encode(), row["password_hash"].encode())


async def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    is_env_account = (
        secrets.compare_digest(credentials.username, ADMIN_USER)
        and secrets.compare_digest(credentials.password, ADMIN_PASSWORD)
    )
    if is_env_account or await _check_db_account(credentials.username, credentials.password):
        return credentials.username
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="인증에 실패했습니다.",
        headers={"WWW-Authenticate": "Basic"},
    )
