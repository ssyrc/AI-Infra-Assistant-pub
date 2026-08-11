"""
admin-console 컨테이너에 마운트된 폴더에서 업로드 후보 파일을 고르는 기능.
'upload_source_dir' 설정(기본 /data/uploads, 관리자 콘솔 설정 탭에서 편집 가능)이 가리키는
디렉토리 밑으로만 접근을 허용한다(경로 탈출 방지). 실제로 그 경로에 뭐가 보이려면
docker-compose에서 호스트 폴더를 그 경로로 bind mount 해둬야 한다.
"""
import os

from fastapi import HTTPException, UploadFile

from config_store import get_config
from uploads import read_upload

DEFAULT_UPLOAD_SOURCE_DIR = "/data/uploads"


async def get_upload_source_root() -> str:
    root = await get_config("upload_source_dir", DEFAULT_UPLOAD_SOURCE_DIR)
    return os.path.realpath(root)


def resolve_under_root(root: str, rel_path: str) -> str:
    """root 밑으로만 벗어나지 않는 절대경로로 만든다. 벗어나면 400."""
    rel_path = (rel_path or "").lstrip("/")
    full = os.path.realpath(os.path.join(root, rel_path))
    if full != root and not full.startswith(root + os.sep):
        raise HTTPException(400, "허용되지 않은 경로입니다.")
    return full


async def read_server_file(rel_path: str, allowed_exts: set[str]) -> tuple[str, bytes, str]:
    """설정된 업로드 폴더 밑의 파일을 읽는다. read_upload와 동일한 검증(크기/매직바이트)을 적용."""
    if not rel_path:
        raise HTTPException(422, "server_path가 비어 있습니다.")
    root = await get_upload_source_root()
    full = resolve_under_root(root, rel_path)
    if not os.path.isfile(full):
        raise HTTPException(404, f"서버 파일을 찾을 수 없습니다: {rel_path} (마운트 확인 필요)")

    filename = os.path.basename(full)
    ext = os.path.splitext(filename)[1].lower()
    if ext not in allowed_exts:
        raise HTTPException(422, f"지원하지 않는 형식입니다. 지원: {', '.join(sorted(allowed_exts))}")

    try:
        max_mb = int(await get_config("upload_max_mb", "50"))
    except (TypeError, ValueError):
        max_mb = 50
    if os.path.getsize(full) > max_mb * 1024 * 1024:
        raise HTTPException(413, f"파일이 너무 큽니다(최대 {max_mb}MB).")

    with open(full, "rb") as f:
        content = f.read()
    if not content:
        raise HTTPException(422, "빈 파일입니다.")
    if ext in (".xlsx", ".docx", ".pptx") and not content.startswith(b"PK"):
        raise HTTPException(422, "파일이 손상되었거나 형식이 올바르지 않습니다.")
    if ext == ".pdf" and not content.startswith(b"%PDF"):
        raise HTTPException(422, "PDF 파일이 손상되었거나 형식이 올바르지 않습니다.")
    return ext, content, filename


async def read_upload_or_server_file(
    file: UploadFile | None, server_path: str | None, allowed_exts: set[str]
) -> tuple[str, bytes, str]:
    """브라우저 업로드(file) 또는 서버 마운트 폴더 선택(server_path) 중 하나를 읽는다."""
    if file is not None:
        ext, content = await read_upload(file, allowed_exts)
        return ext, content, file.filename
    if server_path:
        return await read_server_file(server_path, allowed_exts)
    raise HTTPException(422, "file 또는 server_path 중 하나가 필요합니다.")
