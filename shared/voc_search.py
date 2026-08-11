"""
과거 VOC(질의응답) 이력 하이브리드 검색 — **검색 경로 한 벌**.

`manual_search.py`와 같은 이유로 여기 있다. 예전에는 이 코드가 VOC MCP 안에만 있어서
agent-server가 같은 검색을 하려면 MCP를 HTTP로 부르는 수밖에 없었다. #156에서
"매 질문마다 VOC도 먼저 검색"을 붙이면서 공용으로 뺐다 — 부르는 쪽이 둘(MCP·선검색)이어도
**검색 로직은 하나**여야 결과가 갈리지 않는다.

부수효과 없음: import만으로는 DB에 붙지 않는다.
"""
from db import (
    get_pool, embed_text, vector_literal, rerank, clamp_top_k, clamp_candidates,
)
from pii import mask_record
from config_store import get_config
from retrieval import (
    ts_or_query, expand_query, has_trgm, mmr_dedup, trgm_min_similarity, log_stages,
)

_DSN = "voc_db_dsn"

# 답변에 '사람이 시스템을 직접 확인한 흔적'이 있으면 사용자가 따라 할 수 있는 절차가 아니다.
# 이 목록에 걸리면 `operator`, 안 걸리면 `unknown`이다 — **걸리지 않는 것은 "사용자 건"이라는
# 뜻이 아니라 "모른다"는 뜻이다**(#158). 예전에는 여기서 `user`로 떨어뜨렸고, 그래서 Errors.md에
# 주신 운영자 답변 5개가 전부 `user`가 됐다("조치를 진행하였습니다"가 `조치하였`와 어긋난다).
# 목록이 한계인 이유이고, #157에서 LLM 분류(`voc_classify.py`)로 옮긴 이유다.
# 여기는 백필 전 임시 경로일 뿐이다.
_OPERATOR_HINTS = (
    "확인 결과", "확인결과", "확인해보니", "확인한 결과", "점검 결과", "점검해", "조회해보니",
    "로그를 확인", "로그 확인", "서버에 이상", "장애가 있", "장비 이상", "이상이 있었",
    "재기동", "재시작 처리", "리셋", "복구", "조치했", "조치 완료", "조치하였", "처리했",
    "처리하였", "설정을 변경", "설정 변경", "권한을 부여", "권한 부여", "계정을", "쿼터를 증설",
    "증설", "할당량을 조정", "반영했", "반영하였", "적용했", "적용하였", "삭제했", "삭제하였",
    "담당자가", "운영팀에서", "관리자가", "직접 확인",
)


def classify_handling(answer: str | None) -> str:
    """**아직 분류되지 않은 행에만 쓰는 임시 추론** ("operator" | "unknown").

    사용자 지적: "키워드 사용에 한계가 있더라고." 맞다 — 표현은 무한하고 이 목록은 계속
    뚫린다. 그래서 #157부터 진짜 판정은 `voc_classify.py`(LLM)가 하고 `handled_by` 컬럼에
    저장한다. 이 함수는 그 컬럼이 아직 비어 있을 때(백필 전, 갓 등록된 행)만 쓰인다.

    **`user`를 돌려주지 않는다**(#158). 이 목록은 운영자 신호만 찾는다 — 신호가 없다는 것은
    "사용자가 할 수 있는 건"이라는 뜻이 아니라 **"이 키워드로는 모른다"**는 뜻이다. 예전에는
    여기서 `user`로 떨어뜨려서, 운영자가 조치한 건을 사용자에게 시킬 수 있었다.
    `unknown`은 프롬프트에서 운영자 건과 같이 보수적으로 다뤄진다.

    그 대가로, **백필 전에는 사용자가 직접 해결했던 사례도 `unknown`이 된다** — 그 사례의
    해결 방법을 바로 안내하는 대신 원인 추측으로 안내하게 된다. 백필을 돌리면 풀린다.
    """
    text = (answer or "").replace(" ", "")
    for hint in _OPERATOR_HINTS:
        if hint.replace(" ", "") in text:
            return "operator"
    return "unknown"


async def search_voc_records(query: str, top_k: int = 5, *, vec=None) -> list[dict]:
    """VOC MCP와 agent-server 선검색의 공통 진입점.

    `vec`을 주면 임베딩을 다시 부르지 않는다 (#165) — 선검색은 매뉴얼과 **같은 질의**를 쓰므로
    벡터를 한 번만 만들어 나눠 쓴다.
    """
    if not query or not query.strip():
        return []
    top_k = await clamp_top_k(top_k)
    candidate_k = await clamp_candidates(top_k * 5)
    pool = await get_pool(_DSN)

    if vec is None:
        try:
            vec = await embed_text(query)
        except Exception as e:  # noqa: BLE001
            print(f"[voc] 임베딩 실패, 키워드 검색으로 fallback: {type(e).__name__}: {e}")

    variants = expand_query(query)
    ts_query = ts_or_query(" ".join(variants)) or ts_or_query(query) or "''"
    use_trgm = await has_trgm(pool, _DSN)

    if vec is None:
        rows = await pool.fetch(
            """
            SELECT id, question, answer, created_at, handled_by, handled_by_reason,
                   ts_rank(tsv, to_tsquery('simple', $1)) AS score
            FROM voc_records
            WHERE tsv @@ to_tsquery('simple', $1)
            ORDER BY score DESC
            LIMIT $2
            """,
            ts_query, candidate_k,
        )
    elif use_trgm:
        # 3축 RRF: 벡터(의미) + 키워드(정확 일치) + 3-gram(한국어 부분 일치)
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM voc_records WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $2)) DESC) AS rank
                FROM voc_records WHERE tsv @@ to_tsquery('simple', $2) LIMIT 50
            ),
            trgm_search AS (
                -- similarity()가 아니라 word_similarity(). similarity는 문자열 '전체'의
                -- 3-gram 자카드라 본문이 길수록 0에 수렴해 임계값을 못 넘는다
                -- (= 이 축이 항상 0건이었다).
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY word_similarity($3, question || ' ' || answer) DESC) AS rank
                FROM voc_records
                WHERE word_similarity($3, question || ' ' || answer) >= $4
                LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id, t.id) AS id,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0)
                       + COALESCE(1.0/(60+t.rank),0) AS rrf_score
                FROM vector_search v
                FULL OUTER JOIN keyword_search k ON v.id = k.id
                FULL OUTER JOIN trgm_search t ON COALESCE(v.id, k.id) = t.id
            )
            SELECT r.id, r.question, r.answer, r.created_at,
                   r.handled_by, r.handled_by_reason, fused.rrf_score AS score
            FROM fused
            JOIN voc_records r ON r.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $5
            """,
            vector_literal(vec), ts_query, query, await trgm_min_similarity(), candidate_k,
        )
    else:
        rows = await pool.fetch(
            """
            WITH vector_search AS (
                SELECT id, ROW_NUMBER() OVER (ORDER BY embedding <=> $1::vector) AS rank
                FROM voc_records WHERE embedding IS NOT NULL
                ORDER BY embedding <=> $1::vector LIMIT 50
            ),
            keyword_search AS (
                SELECT id, ROW_NUMBER() OVER (
                    ORDER BY ts_rank(tsv, to_tsquery('simple', $2)) DESC) AS rank
                FROM voc_records WHERE tsv @@ to_tsquery('simple', $2) LIMIT 50
            ),
            fused AS (
                SELECT COALESCE(v.id, k.id) AS id,
                       COALESCE(1.0/(60+v.rank),0) + COALESCE(1.0/(60+k.rank),0) AS rrf_score
                FROM vector_search v FULL OUTER JOIN keyword_search k ON v.id = k.id
            )
            SELECT r.id, r.question, r.answer, r.created_at,
                   r.handled_by, r.handled_by_reason, fused.rrf_score AS score
            FROM fused
            JOIN voc_records r ON r.id = fused.id
            ORDER BY fused.rrf_score DESC
            LIMIT $3
            """,
            vector_literal(vec), ts_query, candidate_k,
        )

    candidates = [dict(r) for r in rows]
    if not candidates:
        log_stages("voc-search", query, 0, 0, 0)
        return []

    # VOC 한 건이 수십만 자인 경우가 있다(긴 처리내용을 통째로 붙여 넣은 문의).
    # 그대로 넘기면 리랭커 입력 한도를 넘기고, 에이전트 컨텍스트도 몇 건 만에 가득 찬다.
    # 판단에 필요한 앞부분만 잘라서 쓴다(원문은 DB에 그대로 남아 있다).
    try:
        max_chars = int(await get_config("voc_result_max_chars", "1500"))
    except (TypeError, ValueError):
        max_chars = 1500

    def clip(text):
        t = text or ""
        if max_chars > 0 and len(t) > max_chars:
            return t[:max_chars] + "\n…(이하 생략)"
        return t

    docs = [f"{clip(c['question'])}\n{clip(c['answer'])}" for c in candidates]
    ranked = await rerank(query, docs, top_k * 2)   # MMR로 걸러질 것을 감안해 여유 있게
    result = []
    for idx, rr_score in ranked:
        item = candidates[idx]
        # 저장된 판정을 쓰고, 아직 분류 전인 행에서만 키워드 추론으로 떨어진다(#157).
        item["handled_by"] = (item.get("handled_by")
                              or classify_handling(item.get("answer")))
        item.pop("handled_by_reason", None)
        item["question"] = clip(item.get("question"))
        item["answer"] = clip(item.get("answer"))
        # 개인·조직 식별 정보는 에이전트에 넘기기 전에 지운다(프롬프트에 원문이 들어가지 않게).
        item = mask_record(item, ("question", "answer"))
        item["rerank_score"] = rr_score
        result.append(item)

    # 사실상 같은 사례가 상위를 다 차지하지 않게 중복 제거(VOC는 유사 문의가 반복 등록된다).
    try:
        threshold = float(await get_config("dedup_similarity", "0.85"))
    except (TypeError, ValueError):
        threshold = 0.85
    picked = mmr_dedup(result, lambda c: f"{c['question']} {c['answer']}", top_k, threshold)
    log_stages("voc-search", query, len(candidates), len(ranked), len(picked))
    return picked
