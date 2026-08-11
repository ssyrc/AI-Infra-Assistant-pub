"""
사용자별(user_id) 장기 메모리 저장소.

설계:
- 채널(Open WebUI / 상위 VOC agent 등)이 달라도 '단일 user_id'로 기억을 공유한다.
- 두 층위:
    · memory_turns   : 대화 턴 원장(스레드 맥락). conversation_id 단위로 최근 N턴을 주입.
    · user_memory    : 여러 대화에서 '증류·요약'된 장기기억(사실/선호/요약). user_id 단위.
- 로드는 recency(최근 턴) + relevance(질문 임베딩으로 user_memory 벡터검색)를 합친다.
- 저장/요약은 응답 후 백그라운드로 수행해 지연을 늘리지 않는다.

임베딩은 shared/db.embed_text(vLLM)를 재사용한다. LLM 요약은 호출자가 summarizer 콜백으로
주입한다(이 모듈은 LLM에 독립적).
"""
from db import get_pool, embed_text, vector_literal

_DSN = "memory_db_dsn"


async def _embed(text: str):
    """(vector_literal|None). 임베딩 실패는 조용히 무시(메모리는 임베딩 없어도 최근성으로 동작)."""
    try:
        vec = await embed_text(text)
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 임베딩 실패, embedding=NULL: {type(e).__name__}: {e}")
        return None
    return vector_literal(vec)


async def load_context(user_id: str, conversation_id: str | None, query: str,
                       recent_turns: int = 8, top_k: int = 5) -> dict:
    """프롬프트에 넣을 메모리를 반환한다.
    {recent: [{role, content}...] 시간순, longterm: [{kind, content}...] 관련도순}."""
    pool = await get_pool(_DSN)

    recent = []
    if conversation_id and recent_turns > 0:
        rows = await pool.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, created_at, id FROM memory_turns
                WHERE conversation_id = $1 AND user_id = $2
                ORDER BY created_at DESC, id DESC
                LIMIT $3
            ) t ORDER BY created_at ASC, id ASC
            """,
            conversation_id, user_id, recent_turns,
        )
        recent = [dict(r) for r in rows]

    longterm = []
    if top_k > 0:
        emb = await _embed(query) if query else None
        if emb is not None:
            rows = await pool.fetch(
                """
                SELECT kind, content FROM user_memory
                WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > now())
                  AND embedding IS NOT NULL
                ORDER BY embedding <=> $2::vector
                LIMIT $3
                """,
                user_id, emb, top_k,
            )
        else:
            rows = await pool.fetch(
                """
                SELECT kind, content FROM user_memory
                WHERE user_id = $1 AND (expires_at IS NULL OR expires_at > now())
                ORDER BY updated_at DESC
                LIMIT $2
                """,
                user_id, top_k,
            )
        longterm = [dict(r) for r in rows]

    return {"recent": recent, "longterm": longterm}


def format_memory_block(longterm: list[dict]) -> str:
    """장기기억을 시스템 지시문에 덧붙일 텍스트로 만든다(없으면 빈 문자열)."""
    if not longterm:
        return ""
    lines = "\n".join(f"- {m['content']}" for m in longterm if m.get("content"))
    if not lines:
        return ""
    # 헤더 문구가 중요하다. 예전엔 "참고용, 확실하지 않으면 근거를 다시 확인"이었는데,
    # 시스템 지시문에 실리는 글이라 모델이 이걸 **근거로** 썼다. "GPU에서 스크래치 사용법"에
    # 예전 CPU 답이 딸려 들어와 CPU 절차를 그대로 낸 사고가 있었다(#122).
    # 근거가 아님을 못박고, 사용법·절차·값에는 쓰지 말라고 명시한다.
    return ("\n\n## 지난 대화에서 요약해 둔 이 사용자 정보 — **근거가 아닙니다**\n"
            "질문을 이해하는 데만 씁니다. 사용법·절차·경로·옵션·설정값을 여기서 가져오지 "
            "마세요. 그런 것은 이번 질문으로 **매뉴얼을 다시 검색해서** 답해야 하며, 아래 내용과 "
            "다르면 검색 결과가 맞습니다. 아래에 답이 있어 보여도 그것으로 답하지 않습니다.\n"
            + lines)


async def record_turns(user_id: str, conversation_id: str | None, source: str | None,
                       turns: list[tuple[str, str]]) -> None:
    """대화 턴들을 원장에 저장한다. turns=[(role, content), ...]."""
    pool = await get_pool(_DSN)
    async with pool.acquire() as conn:
        async with conn.transaction():
            for role, content in turns:
                if not content:
                    continue
                emb = await _embed(content)
                await conn.execute(
                    """
                    INSERT INTO memory_turns (user_id, conversation_id, source, role, content, embedding)
                    VALUES ($1, $2, $3, $4, $5, $6::vector)
                    """,
                    user_id, conversation_id, source, role, content, emb,
                )
            if conversation_id:
                await conn.execute(
                    """
                    INSERT INTO conversation_state (conversation_id, user_id, turn_count, updated_at)
                    VALUES ($1, $2, $3, now())
                    ON CONFLICT (conversation_id) DO UPDATE
                    SET turn_count = conversation_state.turn_count + $3, updated_at = now()
                    """,
                    conversation_id, user_id, len(turns),
                )


async def maybe_summarize(user_id: str, conversation_id: str | None, summarizer,
                          summarize_every: int = 12, ttl_days: int = 180) -> int:
    """대화가 임계 턴 수만큼 쌓이면, 아직 요약 안 한 오래된 턴을 요약해 user_memory로 승격한다.

    summarizer(turns:list[{role,content}]) -> list[str]  (기억할 사실/요약 문장들)
    반환: 새로 추가된 장기기억 개수. conversation_id가 없으면 아무 것도 안 한다."""
    if not conversation_id:
        return 0
    pool = await get_pool(_DSN)
    state = await pool.fetchrow(
        "SELECT turn_count, summarized_upto FROM conversation_state WHERE conversation_id = $1",
        conversation_id,
    )
    if not state:
        return 0

    # 아직 요약 안 한 턴(id > summarized_upto)을 가져온다.
    # 반드시 user_id로도 스코프한다: 외부 호출자가 같은 conversation_id를 서로 다른 사용자에게
    # 재사용해도 남의 턴이 이 사용자의 장기기억으로 섞이지 않게 한다(교차오염 방지).
    pending = await pool.fetch(
        """
        SELECT id, role, content FROM memory_turns
        WHERE conversation_id = $1 AND user_id = $3 AND id > $2
        ORDER BY id ASC
        """,
        conversation_id, state["summarized_upto"], user_id,
    )
    if len(pending) < summarize_every:
        return 0

    turns = [{"role": r["role"], "content": r["content"]} for r in pending]
    try:
        facts = await summarizer(turns)
    except Exception as e:  # noqa: BLE001
        print(f"[memory] 요약 실패(건너뜀): {type(e).__name__}: {e}")
        return 0
    facts = [f.strip() for f in (facts or []) if f and f.strip()]

    max_id = pending[-1]["id"]
    added = 0
    ttl_clause = f"now() + interval '{int(ttl_days)} days'" if ttl_days and int(ttl_days) > 0 else "NULL"
    async with pool.acquire() as conn:
        async with conn.transaction():
            for fact in facts:
                emb = await _embed(fact)
                await conn.execute(
                    f"""
                    INSERT INTO user_memory (user_id, kind, content, embedding, source, expires_at)
                    VALUES ($1, 'summary', $2, $3::vector, 'auto-summary', {ttl_clause})
                    """,
                    user_id, fact, emb,
                )
                added += 1
            await conn.execute(
                "UPDATE conversation_state SET summarized_upto = $1, last_summarized_at = now(), "
                "updated_at = now() WHERE conversation_id = $2",
                max_id, conversation_id,
            )
    return added


# --- 관리(조회/추가/삭제) --------------------------------------------------------
async def list_user_memory(user_id: str, limit: int = 200) -> list[dict]:
    pool = await get_pool(_DSN)
    rows = await pool.fetch(
        "SELECT id, kind, content, source, created_at, updated_at, expires_at "
        "FROM user_memory WHERE user_id = $1 ORDER BY updated_at DESC LIMIT $2",
        user_id, limit,
    )
    return [dict(r) for r in rows]


async def add_user_memory(user_id: str, content: str, kind: str = "fact",
                          source: str = "manual", ttl_days: int = 0) -> int:
    pool = await get_pool(_DSN)
    emb = await _embed(content)
    ttl_clause = f"now() + interval '{int(ttl_days)} days'" if ttl_days and int(ttl_days) > 0 else "NULL"
    return await pool.fetchval(
        f"""
        INSERT INTO user_memory (user_id, kind, content, embedding, source, expires_at)
        VALUES ($1, $2, $3, $4::vector, $5, {ttl_clause}) RETURNING id
        """,
        user_id, kind, content, emb, source,
    )


async def delete_user_memory(user_id: str, memory_id: int | None = None) -> int:
    """memory_id를 주면 그 항목만, 없으면 사용자의 장기기억 전체를 삭제한다(원장 turns 포함)."""
    pool = await get_pool(_DSN)
    if memory_id is not None:
        res = await pool.execute(
            "DELETE FROM user_memory WHERE user_id = $1 AND id = $2", user_id, memory_id)
        return int(res.split()[-1]) if res else 0
    async with pool.acquire() as conn:
        async with conn.transaction():
            r1 = await conn.execute("DELETE FROM user_memory WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM memory_turns WHERE user_id = $1", user_id)
            await conn.execute("DELETE FROM conversation_state WHERE user_id = $1", user_id)
    return int(r1.split()[-1]) if r1 else 0
