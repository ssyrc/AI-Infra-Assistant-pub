"""
VOC MCP - 과거 사용자/운영자 질의응답 이력에서 유사 사례와 해결 방법을 검색.
전용 DB(voc_db)를 사용한다 - Manual MCP와 데이터가 섞이지 않는다.

검색 로직 자체는 `shared/voc_search.py`에 있다 - agent-server의 선검색(#156)이 같은 것을
쓰기 때문이다. 부르는 쪽이 둘이어도 **검색 경로는 하나**여야 결과가 갈리지 않는다.
"""
import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
from voc_search import search_voc_records  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("voc-mcp", stateless_http=True, host="0.0.0.0")


@mcp.tool()
async def search_voc(query: str, top_k: int = 5) -> list[dict]:
    """과거 VOC(질의응답) 이력에서 유사 사례와 해결 방법을 검색한다. 증상·오류·장애용.

    Args:
        query: 질문 또는 증상/오류 메시지. 예: "로그인 시 500 오류"
        top_k: 최대 건수(기본 5)
    Returns:
        각 항목:
          question    — 실제로 접수됐던 문의 원문.
          answer      — 그때 나간 답변. **답변은 이 내용으로만 만든다.**
          handled_by  — "user"(사용자가 직접 해결한 건. 방법을 안내해도 된다) |
                        "operator"(운영자가 시스템을 확인·조치한 건) |
                        "unknown"(답변에 조치 내용이 없어 가리지 못한 건).
                        **"user"가 아닌 건은 그 조치를 사용자에게 시키지 않는다.** 대신 거기
                        적힌 원인을 여러 가능성 중 하나로 제시하고("~였던 경우가 있습니다"),
                        원인마다 사용자가 해 볼 것을 안내한 뒤 마지막에 접수 경로를 알린다.
                        이번 건의 원인이라고 단정하지 않는다.
          created_at
    """
    return await search_voc_records(query, top_k)


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8003))
    uvicorn.run(mcp.streamable_http_app(), host="0.0.0.0", port=port)
