"""
업로드 미리보기 세션 공통 유틸.

파일을 서버가 정한 경로에 저장하고, 세션을 '대상 DB'(dsn 설정 키)에 기록한다.
클라이언트는 upload_id만 알 뿐 저장 경로·확장자·정제 옵션을 결정하지 못한다.
매뉴얼/커맨드 등 파일 업로드가 필요한 여러 탭이 이 모듈을 공유한다.

세션 테이블(upload_sessions)은 각 대상 DB에 마이그레이션으로 만들어져 있어야 한다.
"""
import os
import re
import json
import uuid
import tempfile
from datetime import datetime, timezone

from fastapi import UploadFile, HTTPException

from config_store import get_config
from db import get_pool

_TMP_DIR = tempfile.gettempdir()


async def read_upload(file: UploadFile, allowed_exts: set[str]) -> tuple[str, bytes]:
    """확장자·크기·빈 파일·매직바이트를 검증하고 내용을 읽는다."""
    ext = os.path.splitext(file.filename or "")[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(422, f"지원하지 않는 형식입니다. 지원: {', '.join(sorted(allowed_exts))}")
    try:
        max_mb = int(await get_config("upload_max_mb", "50"))
    except (TypeError, ValueError):
        max_mb = 50
    limit = max_mb * 1024 * 1024

    content = await file.read(limit + 1)
    if len(content) > limit:
        raise HTTPException(413, f"파일이 너무 큽니다(최대 {max_mb}MB).")
    if not content:
        raise HTTPException(422, "빈 파일입니다.")

    # 컨테이너 계열(xlsx/docx/pptx)은 실제 zip인지 매직바이트로 확인
    if ext in (".xlsx", ".docx", ".pptx") and not content.startswith(b"PK"):
        raise HTTPException(422, "파일이 손상되었거나 형식이 올바르지 않습니다.")
    if ext == ".pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(422, "PDF 파일이 손상되었거나 형식이 올바르지 않습니다.")
    return ext, content


async def create_upload_session(dsn_key: str, owner: str, filename: str, ext: str,
                                kind: str, content: bytes, options: dict) -> str:
    """업로드 파일을 서버가 정한 경로에 저장하고 세션을 대상 DB에 기록한다."""
    pool = await get_pool(dsn_key)
    try:
        ttl_min = int(await get_config("upload_session_ttl_minutes", "60"))
    except (TypeError, ValueError):
        ttl_min = 60

    upload_id = uuid.uuid4().hex
    saved_path = os.path.join(_TMP_DIR, f"upload-{upload_id}{ext}")
    with open(saved_path, "wb") as f:
        f.write(content)

    await pool.execute(
        """
        INSERT INTO upload_sessions (upload_id, owner, filename, ext, saved_path, kind, options, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb, now() + ($8 || ' minutes')::interval)
        """,
        upload_id, owner, filename, ext, saved_path, kind,
        json.dumps(options, ensure_ascii=False), str(ttl_min),
    )
    await _cleanup_expired_sessions(pool)
    return upload_id


async def get_upload_session(dsn_key: str, upload_id: str, owner: str, expected_kind: str) -> dict:
    """소유자·만료·종류를 검증하고 세션을 돌려준다."""
    if not re.fullmatch(r"[0-9a-f]{32}", upload_id or ""):
        raise HTTPException(400, "잘못된 upload_id 형식입니다.")
    pool = await get_pool(dsn_key)
    row = await pool.fetchrow("SELECT * FROM upload_sessions WHERE upload_id = $1", upload_id)
    if not row:
        raise HTTPException(404, "업로드 세션이 없거나 만료되었습니다. 다시 업로드하세요.")
    if row["owner"] != owner:
        raise HTTPException(403, "다른 사용자의 업로드 세션입니다.")
    if row["expires_at"] < datetime.now(timezone.utc):
        await delete_upload_session(dsn_key, upload_id)
        raise HTTPException(404, "업로드 세션이 만료되었습니다. 다시 업로드하세요.")
    if row["kind"] != expected_kind:
        raise HTTPException(400, "업로드 종류가 일치하지 않습니다.")
    if not os.path.exists(row["saved_path"]):
        await delete_upload_session(dsn_key, upload_id)
        raise HTTPException(404, "임시 파일이 정리되었습니다. 다시 업로드하세요.")
    return dict(row)


async def delete_upload_session(dsn_key: str, upload_id: str):
    pool = await get_pool(dsn_key)
    row = await pool.fetchrow("SELECT saved_path FROM upload_sessions WHERE upload_id = $1", upload_id)
    if row and os.path.exists(row["saved_path"]):
        try:
            os.unlink(row["saved_path"])
        except OSError:
            pass
    await pool.execute("DELETE FROM upload_sessions WHERE upload_id = $1", upload_id)


async def _cleanup_expired_sessions(pool):
    """만료된 미사용 preview 파일을 정리한다."""
    rows = await pool.fetch("SELECT upload_id, saved_path FROM upload_sessions WHERE expires_at < now()")
    for r in rows:
        if os.path.exists(r["saved_path"]):
            try:
                os.unlink(r["saved_path"])
            except OSError:
                pass
    if rows:
        await pool.execute("DELETE FROM upload_sessions WHERE expires_at < now()")


def load_options(session: dict) -> dict:
    """세션의 options 컬럼을 dict로 돌려준다(문자열/JSONB 모두 대응)."""
    opt = session["options"]
    return opt if isinstance(opt, dict) else json.loads(opt)
