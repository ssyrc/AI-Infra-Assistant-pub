"""
공용 DB / 임베딩 / 리랭킹 클라이언트.

- get_pool(db_key): db_key로 platform_settings에서 DSN을 읽어 MCP별 전용 DB 풀 생성.
- embed_text(): vLLM 임베딩 서버 호출. Redis 캐시가 설정돼 있으면 쿼리 임베딩을 캐시한다.
- rerank(): vLLM(또는 TEI) 리랭커 서버 호출. 설정이 없으면 원본 순서를 그대로 반환한다.
"""
import hashlib
import json
import time

import asyncpg
import httpx

from config_store import get_config

_pools: dict[str, asyncpg.Pool] = {}
_http_client: httpx.AsyncClient | None = None

_redis = None
_redis_next_retry: float = 0.0
REDIS_RETRY_INTERVAL = 60  # 연결 실패 시 이 시간 뒤 재시도(영구 비활성화 방지)


async def get_http_client() -> httpx.AsyncClient:
    """임베딩·리랭커·스케줄러 호출이 공유하는 클라이언트.
    매 호출마다 새로 만들면 TCP/TLS 핸드셰이크 비용이 반복되므로 커넥션 풀을 재사용한다."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=5.0),
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=10),
        )
    return _http_client


async def close_http_client():
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


async def get_pool(db_key: str) -> asyncpg.Pool:
    if db_key not in _pools:
        dsn = await get_config(db_key)
        if not dsn:
            raise RuntimeError(
                f"'{db_key}' DSN이 설정되어 있지 않습니다. 관리자 콘솔 > 설정에서 등록하세요."
            )
        _pools[db_key] = await asyncpg.create_pool(dsn, min_size=1, max_size=10)
    return _pools[db_key]


async def _get_redis():
    """임베딩 캐시용 Redis. 연결 실패 시 영구 비활성화하지 않고 일정 시간 뒤 재시도한다."""
    global _redis, _redis_next_retry
    if _redis is not None:
        return _redis
    if time.time() < _redis_next_retry:
        return None

    url = await get_config("redis_url")
    if not url:
        _redis_next_retry = time.time() + REDIS_RETRY_INTERVAL
        return None
    try:
        import redis.asyncio as aioredis
        client = aioredis.from_url(url, decode_responses=False, socket_connect_timeout=3)
        await client.ping()
        _redis = client
        return _redis
    except Exception as e:  # noqa: BLE001
        print(f"[db] Redis 연결 실패({REDIS_RETRY_INTERVAL}s 후 재시도): {e}")
        _redis_next_retry = time.time() + REDIS_RETRY_INTERVAL
        return None


async def _cache_key(text: str, model: str) -> str:
    """캐시 키에 임베딩 서버·모델·차원·정제 정책 버전을 모두 포함한다.
    (모델이나 정제 정책이 바뀌면 자동으로 캐시가 무효화되도록)"""
    server = await get_config("vllm_embed_base_url", "")
    dim = await get_config("embed_dim", "1024")
    policy = await get_config("clean_policy_version", "1")
    raw = f"{server}|{model}|{dim}|{policy}|{text}"
    return "emb:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


# 임베딩 모델의 최대 입력 길이를 넘기면 서버가 400을 돌려준다(bge-m3는 8192 토큰).
# 매뉴얼 청크는 1500자로 잘라 두지만 VOC는 원문 한 건이 그대로 들어와서 길이 제한이 없었다.
# 한글은 토크나이저에서 대략 글자당 1~1.5토큰이라 4000자면 넉넉히 한도 안쪽이다.
DEFAULT_EMBED_MAX_CHARS = 4000


async def embed_text(text: str) -> list[float]:
    """vLLM /v1/embeddings 호출. Redis가 있으면 캐시한다.

    입력이 모델 한도를 넘으면 잘라서 보낸다. 예전에는 그대로 보내다가 400을 받고
    예외가 위로 튀어, VOC 일괄 등록이 긴 문의 한 건에서 통째로 죽었다(333건 중 16건만 등록).
    """
    model = await get_config("vllm_embed_model", "bge-m3")
    try:
        max_chars = int(await get_config("embed_max_chars", str(DEFAULT_EMBED_MAX_CHARS)))
    except (TypeError, ValueError):
        max_chars = DEFAULT_EMBED_MAX_CHARS
    text = text or ""
    if max_chars > 0 and len(text) > max_chars:
        print(f"[db] 임베딩 입력이 {len(text)}자라 {max_chars}자로 잘라서 보냅니다.")
        text = text[:max_chars]
    redis = await _get_redis()
    key = None
    if redis is not None:
        key = await _cache_key(text, model)
        try:
            cached = await redis.get(key)
            if cached:
                return json.loads(cached)
        except Exception as e:  # noqa: BLE001
            print(f"[db] 캐시 조회 실패(무시): {e}")

    base_url = await get_config("vllm_embed_base_url")
    if not base_url:
        raise RuntimeError("vllm_embed_base_url이 설정되지 않았습니다. 관리자 콘솔 > 설정에서 등록하세요.")
    client = await get_http_client()
    resp = await client.post(
        f"{base_url.rstrip('/')}/embeddings", json={"model": model, "input": text}
    )
    if resp.status_code >= 400:
        # 서버가 왜 거절했는지(길이 초과·모델명 불일치 등)를 그대로 올려 보낸다.
        # 상태 코드만 던지면 호출부에서 원인을 알 수 없다.
        raise RuntimeError(
            f"임베딩 서버가 {resp.status_code}를 반환했습니다(모델 {model}, 입력 {len(text)}자): "
            f"{resp.text[:300]}")
    vec = resp.json()["data"][0]["embedding"]

    if redis is not None and key:
        try:
            ttl = int(await get_config("embed_cache_ttl_seconds", "86400"))
            await redis.set(key, json.dumps(vec), ex=ttl)
        except Exception as e:  # noqa: BLE001
            print(f"[db] 캐시 저장 실패(무시): {e}")
    return vec


async def embed_texts(texts: list[str], batch_size: int = 32,
                      max_consecutive_failures: int = 5) -> list[list[float] | None]:
    """여러 건을 **한 번의 요청에 묶어서** 임베딩한다. 실패한 자리는 None으로 채운다.

    왜 필요한가: 일괄 등록에서 행마다 embed_text()를 부르면 수천 번 왕복한다.
    2천 행이면 요청도 2천 번이라 몇 분이 걸리고, 그동안 화면에는 아무 변화가 없어
    "등록 버튼을 눌러도 아무 일도 안 일어난다"로 보인다.
    OpenAI 호환 임베딩 API는 input에 배열을 받으므로 묶어 보내면 왕복이 수십 번으로 준다.

    한 배치가 통째로 실패하면(입력 하나가 너무 길거나 형식이 이상한 경우) 그 배치만
    한 건씩 다시 시도해 **문제 있는 행만 골라낸다.** 서버 자체가 죽은 경우까지 계속
    두드리지 않도록, 연속 실패가 이어지면 예외를 던진다.
    """
    if not texts:
        return []
    out: list[list[float] | None] = []
    consecutive = 0
    for start in range(0, len(texts), max(1, batch_size)):
        chunk = texts[start:start + max(1, batch_size)]
        try:
            vecs = await _embed_batch(chunk)
            out.extend(vecs)
            consecutive = 0
            continue
        except Exception as e:  # noqa: BLE001
            print(f"[db] 임베딩 배치 실패({start}~{start + len(chunk) - 1}), "
                  f"한 건씩 재시도: {type(e).__name__}: {e}")
        for text in chunk:
            try:
                out.append(await embed_text(text))
                consecutive = 0
            except Exception as e:  # noqa: BLE001
                out.append(None)
                consecutive += 1
                print(f"[db] 임베딩 실패(건너뜀): {type(e).__name__}: {e}")
                if consecutive >= max_consecutive_failures:
                    raise RuntimeError(
                        f"임베딩이 {consecutive}건 연속 실패해 중단했습니다"
                        f"({len(out)}/{len(texts)}건 처리). 마지막 오류: {e}")
    return out


async def _embed_batch(texts: list[str]) -> list[list[float]]:
    """여러 입력을 한 요청으로 임베딩한다(캐시된 것은 건너뛴다)."""
    model = await get_config("vllm_embed_model", "bge-m3")
    try:
        max_chars = int(await get_config("embed_max_chars", str(DEFAULT_EMBED_MAX_CHARS)))
    except (TypeError, ValueError):
        max_chars = DEFAULT_EMBED_MAX_CHARS
    prepared = [(t or "")[:max_chars] if max_chars > 0 else (t or "") for t in texts]

    redis = await _get_redis()
    keys: list[str | None] = [None] * len(prepared)
    result: list[list[float] | None] = [None] * len(prepared)
    todo: list[int] = []
    for i, t in enumerate(prepared):
        if redis is not None:
            keys[i] = await _cache_key(t, model)
            try:
                cached = await redis.get(keys[i])
                if cached:
                    result[i] = json.loads(cached)
                    continue
            except Exception as e:  # noqa: BLE001
                print(f"[db] 캐시 조회 실패(무시): {e}")
        todo.append(i)

    if todo:
        base_url = await get_config("vllm_embed_base_url")
        if not base_url:
            raise RuntimeError(
                "vllm_embed_base_url이 설정되지 않았습니다. 관리자 콘솔 > 설정에서 등록하세요.")
        client = await get_http_client()
        resp = await client.post(
            f"{base_url.rstrip('/')}/embeddings",
            json={"model": model, "input": [prepared[i] for i in todo]},
            timeout=httpx.Timeout(120.0, connect=5.0),   # 묶어 보내므로 넉넉히
        )
        if resp.status_code >= 400:
            raise RuntimeError(
                f"임베딩 서버가 {resp.status_code}를 반환했습니다(모델 {model}, {len(todo)}건): "
                f"{resp.text[:300]}")
        data = resp.json().get("data", [])
        if len(data) != len(todo):
            raise RuntimeError(
                f"임베딩 응답 개수가 맞지 않습니다(요청 {len(todo)}건, 응답 {len(data)}건).")
        # index가 있으면 그대로 쓰고(순서 보장이 명시되지 않은 서버 대비), 없으면 순서대로.
        for pos, item in enumerate(data):
            idx = item.get("index", pos) if isinstance(item, dict) else pos
            if not isinstance(idx, int) or not (0 <= idx < len(todo)):
                idx = pos
            result[todo[idx]] = item["embedding"]

        if redis is not None:
            try:
                ttl = int(await get_config("embed_cache_ttl_seconds", "86400"))
                for i in todo:
                    if keys[i] and result[i] is not None:
                        await redis.set(keys[i], json.dumps(result[i]), ex=ttl)
            except Exception as e:  # noqa: BLE001
                print(f"[db] 캐시 저장 실패(무시): {e}")

    missing = [i for i, v in enumerate(result) if v is None]
    if missing:
        raise RuntimeError(f"임베딩 결과가 비어 있는 항목이 있습니다: {missing[:5]}")
    return result  # type: ignore[return-value]


async def rerank(query: str, documents: list[str], top_k: int) -> list[tuple[int, float]]:
    """리랭커로 (원본인덱스, 점수) 상위 top_k를 반환한다.

    안정성 원칙: 리랭킹은 '품질 향상'이지 '필수 경로'가 아니다.
    미설정·타임아웃·오류·형식 불일치 등 어떤 문제가 생겨도 예외를 던지지 않고
    입력 순서(=RRF 순위) 상위 top_k로 fallback한다.
    """
    if not documents:
        return []
    fallback = [(i, 0.0) for i in range(min(top_k, len(documents)))]

    base_url = await get_config("rerank_base_url")
    provider = (await get_config("rerank_provider", "tei") or "tei").lower()
    if not base_url or provider == "none":
        return fallback

    model = await get_config("rerank_model", "bge-reranker-v2-m3")
    try:
        timeout = float(await get_config("rerank_timeout_seconds", "5"))
    except (TypeError, ValueError):
        timeout = 5.0

    try:
        client = await get_http_client()
        if provider == "vllm":
            # vLLM score/rerank API
            url = f"{base_url.rstrip('/')}/rerank"
            payload = {"model": model, "query": query, "documents": documents}
        else:
            # TEI(Text Embeddings Inference) rerank API
            url = f"{base_url.rstrip('/')}/rerank"
            payload = {"query": query, "texts": documents, "raw_scores": False}

        resp = await client.post(url, json=payload, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:  # noqa: BLE001
        print(f"[rerank] 실패, RRF 순위로 fallback: {type(e).__name__}: {e}")
        return fallback

    # 응답 형식 호환: {"results":[{index, relevance_score}]} 또는 [{index, score}]
    if isinstance(data, dict):
        raw = data.get("results", [])
    elif isinstance(data, list):
        raw = data
    else:
        return fallback

    scored: list[tuple[int, float]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        score = item.get("relevance_score", item.get("score"))
        # index 타입·범위 검증 (잘못된 응답으로 IndexError가 나지 않도록)
        if not isinstance(idx, int) or not (0 <= idx < len(documents)):
            continue
        try:
            score = float(score)
        except (TypeError, ValueError):
            continue
        scored.append((idx, score))

    if not scored:
        return fallback
    scored.sort(key=lambda x: x[1], reverse=True)

    # 관련 없는 문서 걸러내기: 리랭커 점수가 기준 미만이면 아예 돌려주지 않는다.
    # RRF 상위라는 건 '후보 중 상대적으로 나은 것'일 뿐이라, 질문과 무관한 문서라도
    # 후보가 적으면 그냥 1등이 된다(그걸 근거로 답하면 엉뚱한 답이 나온다).
    # 리랭커가 실제로 점수를 매긴 경우에만 적용한다 - fallback(전부 0.0)에는 적용하지 않는다.
    try:
        min_score = float(await get_config("rerank_min_score", "0.05"))
    except (TypeError, ValueError):
        min_score = 0.05
    if min_score > 0:
        kept = [(i, sc) for i, sc in scored if sc >= min_score]
        if len(kept) < len(scored):
            print(f"[rerank] 관련도 미달 {len(scored) - len(kept)}건 제외"
                  f"(기준 {min_score}, 최고점 {scored[0][1]:.4f})")
        scored = kept
    return scored[:top_k]


def vector_literal(vec: list[float]) -> str:
    """pgvector 쿼리 파라미터용 문자열 변환: '[0.1,0.2,...]'"""
    return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"


async def clamp_top_k(top_k: int) -> int:
    """LLM이 비정상적으로 큰 top_k를 넘겨도 DB/컨텍스트가 폭발하지 않도록 상한을 건다."""
    try:
        max_k = int(await get_config("search_max_top_k", "20"))
    except (TypeError, ValueError):
        max_k = 20
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = 5
    return max(1, min(k, max_k))


async def clamp_candidates(candidate_k: int) -> int:
    try:
        max_c = int(await get_config("search_max_candidates", "100"))
    except (TypeError, ValueError):
        max_c = 100
    return max(1, min(int(candidate_k), max_c))
