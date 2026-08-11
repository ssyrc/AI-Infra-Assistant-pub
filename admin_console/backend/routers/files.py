"""
관리자 콘솔에 마운트된 업로드 후보 폴더 목록 API. 매뉴얼/VOC/커맨드 카탈로그 업로드 화면에서
'서버 파일에서 선택'에 쓴다. 'upload_source_dir' 설정이 가리키는 디렉토리 밑만 노출한다.
"""
import os

from fastapi import APIRouter, Depends, HTTPException

from auth import require_admin
from server_files import get_upload_source_root, resolve_under_root

router = APIRouter(prefix="/api/files", tags=["files"])


@router.get("")
async def list_files(path: str = "", admin: str = Depends(require_admin)):
    root = await get_upload_source_root()
    full = resolve_under_root(root, path)
    if not os.path.isdir(full):
        raise HTTPException(
            404, f"'{root}' 폴더를 찾을 수 없습니다. 서버에 마운트됐는지(docker-compose volumes) 확인하세요.")
    entries = []
    for name in sorted(os.listdir(full)):
        p = os.path.join(full, name)
        entries.append({
            "name": name,
            "path": os.path.relpath(p, root),
            "is_dir": os.path.isdir(p),
            "size": os.path.getsize(p) if os.path.isfile(p) else None,
        })
    return {"root": root, "path": path, "entries": entries}
