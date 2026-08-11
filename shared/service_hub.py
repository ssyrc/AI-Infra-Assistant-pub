"""
Service Hub MCP 클라이언트 - 유사 VOC 조회(similar_voc) 후처리용.

에이전트 툴로 노출하지 않고, /v1/voc/query에서 '직접' 호출해 결과를 정해진 형태로 매핑한다
(결정적 출력). Service Hub MCP는 원격 streamable-http MCP 서버다.

가이드 기준 반환 형태:
  rag_keyword_search / rag_filtered_search -> {success, data:{total, results:[{title, content, score}]}}
  주의: 검색 결과에는 voc_id / system이 없다. 따라서 similar_voc의 voc_id/system은
        '있으면' 채우고(실서버가 더 많은 필드를 줄 수 있어 방어적으로 여러 키를 시도),
        system은 필터로 쓴 system_name을 fallback으로 쓴다. reason은 content 스니펫으로 만든다.

방화벽/URL 미설정 시(service_hub_mcp_url 비어있음) 조용히 빈 리스트를 반환한다.
"""
import json
import asyncio

from config_store import get_config

try:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client
    _MCP_OK = True
except Exception:  # noqa: BLE001
    _MCP_OK = False


def _extract_results(call_result) -> list:
    """call_tool 결과에서 data.results 리스트를 뽑는다(structuredContent 우선, 없으면 텍스트 JSON)."""
    data = None
    sc = getattr(call_result, "structuredContent", None)
    if isinstance(sc, dict):
        data = sc
    else:
        for block in getattr(call_result, "content", []) or []:
            txt = getattr(block, "text", None)
            if txt:
                try:
                    data = json.loads(txt)
                    break
                except (ValueError, TypeError):
                    continue
    if not isinstance(data, dict):
        return []
    inner = data.get("data") if isinstance(data.get("data"), dict) else data
    results = inner.get("results")
    return results if isinstance(results, list) else []


def _to_similar(results: list, fallback_system: str | None) -> list[dict]:
    """검색 결과를 similar_voc 형태 {voc_id?, title, system?, reason}로 매핑한다."""
    out = []
    for r in results:
        if not isinstance(r, dict):
            continue
        title = (r.get("title") or "").strip()
        content = (r.get("content") or "").strip()
        score = r.get("score")
        vid = r.get("voc_id") or r.get("id") or r.get("doc_id")
        system = r.get("system") or r.get("system_name") or fallback_system
        if content:
            reason = content[:150] + ("…" if len(content) > 150 else "")
        elif score is not None:
            reason = f"키워드 검색 유사도 {score}"
        else:
            reason = "키워드 검색 유사 문서"
        item = {"title": title, "reason": reason}
        if vid:
            item["voc_id"] = str(vid)
        if system:
            item["system"] = system
        out.append(item)
    return out


async def search_similar_voc(query: str, system_name: str | None = None,
                             num_result: int = 3, timeout: float = 15.0) -> list[dict]:
    """Service Hub MCP로 유사 VOC를 조회해 similar_voc 리스트를 돌려준다.
    system_name이 있으면 rag_filtered_search(같은 시스템으로 좁힘), 없으면 rag_keyword_search."""
    if not _MCP_OK or not query or not query.strip() or num_result <= 0:
        return []
    url = await get_config("service_hub_mcp_url")
    if not url:   # 방화벽 미개통/미설정 -> 조용히 생략
        return []

    args = {"query": query[:1000], "num_result_doc": int(num_result)}
    tool = "rag_keyword_search"
    if system_name:
        args["system_name"] = system_name
        tool = "rag_filtered_search"

    async def _call():
        async with streamablehttp_client(url) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool(tool, args)

    try:
        result = await asyncio.wait_for(_call(), timeout=timeout)
    except Exception as e:  # noqa: BLE001
        print(f"[service-hub] similar_voc 조회 실패(무시): {type(e).__name__}: {e}")
        return []
    return _to_similar(_extract_results(result), system_name)
