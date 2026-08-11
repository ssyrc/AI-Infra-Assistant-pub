"""
관리자 콘솔 계정 관리 API (platform_config.admin_accounts).
.env의 ADMIN_USER 계정은 잠금 방지용 기본 계정이라 여기서 만들거나 지울 수 없다.
"""
import re

import bcrypt
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from auth import ADMIN_USER, get_config_pool, require_admin

router = APIRouter(prefix="/api/accounts", tags=["accounts"])

USERNAME_RE = re.compile(r"^[a-z][a-z0-9_.-]{2,31}$")


def _validate_password(password: str) -> None:
    if len(password) < 8:
        raise HTTPException(400, "비밀번호는 8자 이상이어야 합니다.")


class AccountIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    password: str


@router.get("")
async def list_accounts(admin: str = Depends(require_admin)):
    pool = await get_config_pool()
    rows = await pool.fetch(
        "SELECT username, created_by, created_at FROM admin_accounts ORDER BY created_at")
    result = [dict(r) for r in rows]
    result.insert(0, {"username": ADMIN_USER, "created_by": None, "created_at": None, "env_account": True})
    return result


@router.post("")
async def create_account(body: AccountIn, admin: str = Depends(require_admin)):
    if not USERNAME_RE.match(body.username):
        raise HTTPException(400, "아이디는 소문자로 시작하고 소문자/숫자/._- 만 3~32자로 써주세요.")
    if body.username == ADMIN_USER:
        raise HTTPException(400, "이 아이디는 .env 기본 계정과 겹칩니다.")
    _validate_password(body.password)

    pool = await get_config_pool()
    exists = await pool.fetchval("SELECT 1 FROM admin_accounts WHERE username = $1", body.username)
    if exists:
        raise HTTPException(400, "이미 있는 아이디입니다.")

    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    await pool.execute(
        "INSERT INTO admin_accounts (username, password_hash, created_by) VALUES ($1, $2, $3)",
        body.username, pw_hash, admin,
    )
    return {"ok": True}


@router.put("/{username}/password")
async def change_password(username: str, body: PasswordIn, admin: str = Depends(require_admin)):
    if username == ADMIN_USER:
        raise HTTPException(400, ".env 기본 계정의 비밀번호는 .env에서 바꾸세요.")
    _validate_password(body.password)

    pool = await get_config_pool()
    pw_hash = bcrypt.hashpw(body.password.encode(), bcrypt.gensalt()).decode()
    result = await pool.execute(
        "UPDATE admin_accounts SET password_hash = $1 WHERE username = $2", pw_hash, username)
    if result == "UPDATE 0":
        raise HTTPException(404, "존재하지 않는 계정입니다.")
    return {"ok": True}


@router.delete("/{username}")
async def delete_account(username: str, admin: str = Depends(require_admin)):
    if username == ADMIN_USER:
        raise HTTPException(400, ".env 기본 계정은 지울 수 없습니다(잠금 방지용).")
    pool = await get_config_pool()
    await pool.execute("DELETE FROM admin_accounts WHERE username = $1", username)
    return {"ok": True}
