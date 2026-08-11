"""
매뉴얼 청크 검색 - Manual MCP와 관리자 콘솔 '검색 테스트'가 **같은 코드**를 쓴다.

왜 공유하나: 예전에는 콘솔 진단이 자기만의 SQL(plainto_tsquery 2축)을 갖고 있어서,
진단 결과가 실제 검색과 달랐다. "콘솔에선 잘 나오는데 챗봇은 못 찾는다"의 원인을
찾을 수 없는 구조였다. 검색 경로는 하나만 둔다.

검색 축(3축 RRF, k=60):
  1) 벡터   - bge-m3 임베딩 코사인 (의미)
  2) 키워드 - to_tsvector('simple') + OR to_tsquery (정확 일치)
  3) 3-gram - word_similarity(질의, 본문) (한국어 부분 일치·오타)
이후 cross-encoder 리랭킹 → 관련도 하한 → MMR 중복 제거 → (옵션) 이웃 청크 확장.
"""
from db import embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates
from config_store import get_config
from pii import mask_accounts
from retrieval import (
    ts_or_query, expand_query, has_trgm, mmr_dedup, trgm_min_similarity, log_stages,
)

_DSN = "manual_db_dsn"


def with_context(c: dict) -> str:
    """리랭커에 넘길 텍스트에 '등록 제목 > 원본 문서 이름 > 섹션 제목'을 앞에 붙인다
    (Contextual retrieval).

    청크 본문만 보면 무엇에 대한 문서인지 알 수 없어 관련도 판단이 흐려진다.
    한 번에 여러 가이드 문서를 올린 경우(활용 가이드 메뉴 전체 등) 등록 제목만으로는
    문서가 구분되지 않으므로 doc_title까지 넣는다 - 관리자 콘솔의 임베딩 입력과 같은 형태다.
    """
    head = " > ".join(x for x in (c.get("title"), c.get("doc_title"),
                                  c.get("section_title")) if x)
    return f"{head}\n{c['chunk_text']}" if head else c["chunk_text"]


def full_reference(c: dict) -> str | None:
    """이 청크를 찾아갈 수 있는 전체 경로. reference_path(메뉴까지) + doc_title(문서 이름).

    관리자는 매뉴얼 탭에 메뉴 경로까지만 넣는다("… > 활용 가이드"). 그 메뉴 안에 어떤 문서가
    있는지는 행마다 다르므로(doc_title), 둘을 이어 붙인 문자열도 함께 준다.

    다만 **이것만 주면 LLM이 줄여서 옮긴다** - URL이 든 긴 경로를 "슈퍼컴 Portal > 활용 가이드"
    처럼 요약해 버려 사용자가 문서를 찾을 수 없었다. 그래서 검색 결과에는
    `guide_location`(경로)과 `guide_document`(문서 이름)를 **따로** 실어, 지시문이 정해진
    두 줄 형식으로 그대로 옮겨 적게 한다.
    """
    parts = [x for x in (c.get("reference_path"), c.get("doc_title")) if x]
    return " > ".join(parts) if parts else None


async def _candidates(pool, query: str, candidate_k: int, vec=None) -> tuple[str, list[dict]]:
    if vec is None:
        try:
            vec = await embed_text(query)
        except Exception as e:  # noqa: BLE001
            print(f"[manual-search] 임베딩 실패, 키워드 검색으로 fallback: "
                  f"{type(e).__name__}: {e}")

    # 한국어는 조사/어미 때문에 AND 질의(plainto_tsquery)로는 거의 안 잡힌다 -> OR 질의로 만든다.
    variants = expand_query(query)
    ts_query = ts_or_query(" ".join(variants)) or ts_or_query(query) or "''"

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT c.id, c.seq, c.manual_file_id, c.doc_title, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version, f.reference_path,
                   ts_rank(c.tsv, to_tsquery('simple', $1)) AS score
            FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $1)
            ORDER BY score DESC LIMIT $2
            """,
            ts_query, candidate_k,
        )
        return "키워드 전용(임베딩 실패)", [dict(r) for r in rows]

    if await has_trgm(pool, _DSN):
        # word_similarity는 GIN 인덱스를 타지 않는다(연산자 `<%`만 인덱스 사용).
        # 하지만 인덱스를 타는 `%`는 위 주석대로 긴 청크에서 절대 참이 되지 않아 무의미했다.
        # 청크 수 규모(수천~수만)에서는 순차 평가가 수십 ms 수준이라 정확도를 택한다.
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.embedding IS NOT NULL
                ORDER BY c.embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(c.tsv, to_tsquery('simple', $2)) DESC) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $2)
                LIMIT 50
            ),
            trgm_search AS (
                SELECT c.id, ROW_NUMBER() OVER (
                    ORDER BY word_similarity($3, c.chunk_text) DESC) AS rank
                FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
                WHERE f.status = 'published' AND word_similarity($3, c.chunk_text) >= $4
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id, t.id) AS id,
                       COALESCE(1.0/(60+v.rank),0) AS vrrf,
                       COALESCE(1.0/(60+k.rank),0) AS krrf,
                       COALESCE(1.0/(60+t.rank),0) AS trrf,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0)
                       + COALESCE(1.0/(60+t.rank),0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
                FULL OUTER JOIN trgm_search t ON COALESCE(v.id, k.id) = t.id
            )
            SELECT c.id, c.seq, c.manual_file_id, c.doc_title, c.section_title, c.page_no, c.chunk_text,
                   f.title, f.filename, f.version, f.reference_path,
                   fused.rrf_score AS score, fused.vrrf, fused.krrf, fused.trrf
            FROM fused
            JOIN manual_chunks c ON c.id = fused.id
            JOIN manual_files f ON f.id = c.manual_file_id
            ORDER BY fused.rrf_score DESC LIMIT $5
            """,
            vector_literal(vec), ts_query, query, await trgm_min_similarity(), candidate_k,
        )
        return "하이브리드 3축(의미+키워드+3gram)", [dict(r) for r in rows]

    rows = await pool.fetch(
        """
        WITH vector_search AS (
            SELECT c.id, ROW_NUMBER() OVER (ORDER BY c.embedding <=> $1::vector) AS rank
            FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.embedding IS NOT NULL
            ORDER BY c.embedding <=> $1::vector LIMIT 50
        ),
        keyword_search AS (
            SELECT c.id, ROW_NUMBER() OVER (
                ORDER BY ts_rank(c.tsv, to_tsquery('simple', $2)) DESC) AS rank
            FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
            WHERE f.status = 'published' AND c.tsv @@ to_tsquery('simple', $2)
            LIMIT 50
        ),
        fused AS (
            SELECT COALESCE(v.id, k.id) AS id,
                   COALESCE(1.0/(60+v.rank),0) AS vrrf,
                   COALESCE(1.0/(60+k.rank),0) AS krrf, 0.0 AS trrf,
                   COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0) AS rrf_score
            FROM vector_search v FULL OUTER JOIN keyword_search k ON v.id = k.id
        )
        SELECT c.id, c.seq, c.manual_file_id, c.doc_title, c.section_title, c.page_no, c.chunk_text,
               f.title, f.filename, f.version, f.reference_path,
               fused.rrf_score AS score, fused.vrrf, fused.krrf, fused.trrf
        FROM fused
        JOIN manual_chunks c ON c.id = fused.id
        JOIN manual_files f ON f.id = c.manual_file_id
        ORDER BY fused.rrf_score DESC LIMIT $3
        """,
        vector_literal(vec), ts_query, candidate_k,
    )
    return "하이브리드 2축(pg_trgm 없음)", [dict(r) for r in rows]


async def _attach_neighbors(pool, results: list[dict], window: int) -> list[dict]:
    """검색된 청크의 앞뒤 청크를 같은 문서에서 가져와 본문에 이어 붙인다.

    왜 필요한가: 절차 문서는 한 단계가 한 청크(엑셀/CSV는 한 행 = 한 페이지)라, 검색이
    2단계와 4단계만 집어오면 답변에서 3단계가 통째로 사라진다. 실제로 "슈퍼컴 계정 신청"
    답변에서 중간 단계가 빠지는 문제가 이것이었다. 관련도 순서는 리랭킹 결과 그대로 두고,
    각 결과의 '읽을 범위'만 넓힌다.
    """
    if window <= 0 or not results:
        return results
    wanted = sorted({(r["manual_file_id"], r["seq"] + d)
                     for r in results for d in range(-window, window + 1)})
    rows = await pool.fetch(
        """
        SELECT c.manual_file_id, c.seq, c.page_no, c.doc_title, c.section_title, c.chunk_text
        FROM manual_chunks c JOIN manual_files f ON f.id = c.manual_file_id
        WHERE f.status = 'published'
          AND (c.manual_file_id, c.seq) IN (SELECT * FROM unnest($1::int[], $2::int[]))
        ORDER BY c.manual_file_id, c.seq
        """,
        [fid for fid, _ in wanted], [seq for _, seq in wanted],
    )
    by_key = {(r["manual_file_id"], r["seq"]): dict(r) for r in rows}
    for item in results:
        fid, seq = item["manual_file_id"], item["seq"]
        parts, pages = [], []
        for d in range(-window, window + 1):
            nb = by_key.get((fid, seq + d))
            if not nb:
                continue
            # 한 번에 여러 문서를 올린 경우(활용 가이드 메뉴 전체 등) seq는 파일 전체에서
            # 이어지므로, 이웃이 '다른 원본 문서'의 첫/마지막 장일 수 있다. 그걸 이어 붙이면
            # 서로 상관없는 두 가이드가 한 근거로 섞인다 - 문서 경계를 넘지 않는다.
            if nb.get("doc_title") != item.get("doc_title"):
                continue
            parts.append(nb["chunk_text"])
            if nb["page_no"] is not None:
                pages.append(nb["page_no"])
        if len(parts) > 1:
            item["chunk_text"] = "\n".join(parts)
            item["expanded_with_neighbors"] = True
            if pages:
                item["page_range"] = [min(pages), max(pages)]
    return results


async def search_manual_chunks(query: str, top_k: int = 5, *,
                               with_neighbors: bool = True, vec=None
                               ) -> tuple[str, list[dict]]:
    """(mode, 결과리스트)를 돌려준다. Manual MCP와 콘솔 검색 테스트의 공통 진입점.

    `vec`을 주면 임베딩을 다시 부르지 않는다 (#165). 선검색은 **같은 질의로** 매뉴얼과 VOC를
    동시에 찾는데, 각자 임베딩을 부르면 같은 문장을 두 번 벡터로 만든다. 임베딩 서버 왕복이
    선검색 지연(2~3초)의 큰 몫이라 한 번으로 줄인다.
    """
    from db import get_pool

    if not query or not query.strip():
        return "빈 질의", []
    top_k = await clamp_top_k(top_k)
    # 후보 수를 top_k에만 매달면, **프롬프트에 몇 건을 넣을지**를 줄인 것이 곧 **검색 자체의
    # 회수율**을 줄인다. 선검색(top_k=3)이 모델의 직접 검색(top_k=5)을 대신하게 되면서
    # 후보가 25 → 15로 떨어졌고, 그때부터 "예전엔 나오던 문서가 안 나온다"가 됐다(#179).
    # 후보를 더 보는 비용은 DB 시간뿐이다(리랭커에는 여전히 top_k*2만 남는다).
    candidate_k = await clamp_candidates(max(top_k * 5, 25))
    pool = await get_pool(_DSN)

    mode, candidates = await _candidates(pool, query, candidate_k, vec)
    if not candidates:
        log_stages("manual-search", query, 0, 0, 0)
        return mode, []

    ranked = await rerank(query, [with_context(c) for c in candidates], top_k * 2)
    ordered = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        item["rerank_score"] = rr_score
        ordered.append(item)

    try:
        threshold = float(await get_config("dedup_similarity", "0.85"))
    except (TypeError, ValueError):
        threshold = 0.85
    picked = mmr_dedup(ordered, lambda c: c["chunk_text"], top_k, threshold)
    log_stages("manual-search", query, len(candidates), len(ranked), len(picked))

    if with_neighbors:
        try:
            window = int(await get_config("manual_neighbor_window", "1"))
        except (TypeError, ValueError):
            window = 1
        picked = await _attach_neighbors(pool, picked, max(0, min(window, 3)))

    # 결과 하나가 너무 길면 LLM 컨텍스트가 몇 건 만에 가득 찬다(실제로 32768토큰을 넘겨
    # ContextWindowExceededError가 났다). 이웃 청크까지 붙은 뒤라 더 길어지므로 여기서 자른다.
    try:
        max_chars = int(await get_config("manual_result_max_chars", "1500"))
    except (TypeError, ValueError):
        max_chars = 1500

    # 답변에 그대로 옮겨 적을 값을 만들어 준다. LLM이 조합하게 두면 순서를 바꾸거나 줄인다.
    #  · guide_location  = 관리자가 넣은 문서 위치(URL 포함). **줄이지 말고 그대로** 써야 한다.
    #  · guide_document  = 그 위치 안의 문서 이름.
    for item in picked:
        item["reference"] = full_reference(item)
        item["guide_location"] = item.get("reference_path") or ""
        item["guide_document"] = item.get("doc_title") or item.get("title") or ""
        # 매뉴얼에도 남의 계정·이메일이 예시로 박혀 있다("OOO.OO 계정으로 접속"). 그대로
        # 넘기면 모델이 그것을 **질문한 사람의 계정인 양** 답에 옮긴다(실제로 그랬다).
        # 조직명·직급은 남긴다 - 절차의 일부라서 지우면 어디에 신청할지 알 수 없게 된다.
        # 검색이 끝난 뒤에 가리므로 검색 품질(벡터·키워드·리랭킹)에는 영향이 없다.
        item["chunk_text"] = mask_accounts(item.get("chunk_text"))
        text = item.get("chunk_text") or ""
        if max_chars > 0 and len(text) > max_chars:
            item["chunk_text"] = text[:max_chars] + "\n…(이하 생략, 더 필요하면 get_document)"
    return mode, picked
