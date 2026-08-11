"""
Manual MCP - 사용자 가이드/매뉴얼(엑셀·PPT·워드 → 청크화된 문서) RAG 검색.
관리자 콘솔에서 발행(status='published')한 문서만 검색 대상이 된다.
전용 DB(manual_db)를 사용한다 - VOC/Command/System MCP와 데이터가 섞이지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from db import get_pool  # noqa: E402
from manual_search import search_manual_chunks  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("manual-mcp", stateless_http=True, host="0.0.0.0")


@mcp.tool()
async def search_manual(query: str, top_k: int = 5) -> list[dict]:
    """사내 매뉴얼·가이드에서 사용법·설정·절차·정책의 근거 문단을 검색한다.

    Args:
        query: 자연어 질문 또는 키워드. 예: "배치 스케줄 등록 방법"
        top_k: 최대 문단 수(기본 5)
    Returns:
        각 항목:
          guide_location  — **문서 안내에 그대로 옮겨 쓸 위치**(관리자가 넣은 메뉴 경로/URL).
                            줄이거나 요약하지 말고 한 글자도 바꾸지 않는다.
          guide_document  — 그 위치 안의 문서 이름. 위치와 **함께** 안내한다.
          chunk_text      — 근거 문단(앞뒤 문단 포함). 답변은 이 내용으로만 만든다.
          doc_title, section_title, page_no, manual_file_id, reference(위치+문서명 합본)
    """
    _mode, results = await search_manual_chunks(query, top_k, with_neighbors=True)
    return results


@mcp.tool()
async def get_document(manual_file_id: int, offset: int = 0, limit: int = 20,
                       max_chars: int = 8000) -> dict:
    """매뉴얼 문서를 순서대로 이어 읽는다. search_manual 결과만으로 맥락이 부족할 때만 쓴다.

    Args:
        manual_file_id: search_manual 결과의 manual_file_id
        offset: 건너뛸 청크 수(이어 읽을 때 이전 응답의 next_offset)
        limit: 최대 청크 수(기본 20)
        max_chars: 반환 길이 상한(기본 8000자)
    Returns:
        total_chunks, returned, has_more, next_offset, truncated_by_max_chars, chunks[].
    """
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 50))
    max_chars = max(500, min(int(max_chars), 20000))

    pool = await get_pool("manual_db_dsn")
    total = await pool.fetchval(
        """
        SELECT count(*) FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.manual_file_id = $1 AND f.status = 'published'
        """,
        manual_file_id,
    )
    rows = await pool.fetch(
        """
        SELECT c.seq, c.doc_title, c.section_title, c.page_no, c.chunk_text
        FROM manual_chunks c
        JOIN manual_files f ON f.id = c.manual_file_id
        WHERE c.manual_file_id = $1 AND f.status = 'published'
        ORDER BY c.seq, c.page_no NULLS LAST, c.id
        OFFSET $2 LIMIT $3
        """,
        manual_file_id, offset, limit,
    )

    chunks, used, truncated = [], 0, False
    for r in rows:
        text = r["chunk_text"]
        if used + len(text) > max_chars:
            remain = max_chars - used
            if remain > 200:
                chunks.append({**dict(r), "chunk_text": text[:remain] + " …(잘림)"})
                used = max_chars
            truncated = True
            break
        chunks.append(dict(r))
        used += len(text)

    returned_end = offset + len(chunks)
    return {
        "total_chunks": total or 0,
        "offset": offset,
        "returned": len(chunks),
        "has_more": returned_end < (total or 0) or truncated,
        "next_offset": returned_end,
        "truncated_by_max_chars": truncated,
        "chunks": chunks,
    }


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8001))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
