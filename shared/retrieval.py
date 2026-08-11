"""
검색 품질 공통 유틸 (Manual/VOC/Command MCP가 함께 쓴다).

여기 있는 것:
1) 한국어 키워드 검색  - to_tsquery를 OR로 만들고, 조사/어미를 떼서 매칭률을 올린다.
2) 쿼리 확장          - 원문 + 어미 제거본 + 핵심어만 남긴 변형을 만들어 검색 축을 넓힌다.
3) 3-gram 유사도 축    - pg_trgm이 있으면 문자 3-gram 검색을 세 번째 후보 축으로 쓴다.
4) MMR 중복 제거      - 거의 같은 청크가 상위를 다 차지하지 않게 걸러낸다.

왜 이렇게 하나(폐쇄망 제약):
- 형태소 분석기(mecab-ko 등)는 새 패키지·사전 설치가 필요해 오프라인 배포가 번거롭다.
  `pg_trgm`은 Postgres 기본 contrib라 추가 설치 없이 CREATE EXTENSION만으로 쓸 수 있고,
  한국어처럼 공백 토큰화가 무의미한 언어에서 문자 n-gram이 실제로 잘 동작한다.
- 그래도 형태소 수준은 아니므로, 조사/어미 제거는 규칙으로 보완한다("접근하려면" -> "접근").
"""
import re

# 조사·어미. 긴 것부터 떼어야 "하려면"이 "면"으로 잘리지 않는다.
_SUFFIXES = sorted([
    "하려면", "하려고", "하는법", "하는 법", "합니까", "습니다", "하나요", "할까요", "해줘", "해주세요",
    "하기", "하는", "해서", "하고", "한다", "했다", "됩니다", "입니다", "이에요", "예요",
    "에서는", "에서의", "으로는", "에게서", "께서는", "이라는", "라는", "부터", "까지", "에서",
    "으로", "로서", "로써", "에게", "한테", "께서", "이나", "거나", "이랑", "하고",
    "은", "는", "이", "가", "을", "를", "의", "에", "도", "만", "과", "와", "로", "야", "요",
], key=len, reverse=True)

_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣_.-]+")
# to_tsquery에 그대로 넣으면 안 되는 문자
_UNSAFE_TS = re.compile(r"[^0-9A-Za-z가-힣_]")


def strip_suffix(token: str) -> str:
    """한국어 토큰에서 조사/어미를 한 번 떼어 낸다(2글자 이상 남을 때만)."""
    for suf in _SUFFIXES:
        if token.endswith(suf) and len(token) - len(suf) >= 2:
            return token[: -len(suf)]
    return token


def tokens_of(query: str) -> list[str]:
    """검색에 쓸 토큰(조사/어미 제거본 포함, 순서 유지·중복 제거)."""
    out: list[str] = []
    for raw in _TOKEN_RE.findall(query or ""):
        for cand in (raw, strip_suffix(raw)):
            c = cand.strip()
            if len(c) >= 2 and c.lower() not in [o.lower() for o in out]:
                out.append(c)
    return out


def ts_or_query(query: str) -> str:
    """to_tsquery('simple', ...)에 넣을 OR 질의 문자열.

    plainto_tsquery는 토큰을 전부 AND로 묶어서, 한국어 문장처럼 조사가 붙은 토큰이 하나라도
    문서에 없으면 결과가 0건이 된다("gpu & 노드 & 접근하려면"). OR로 묶어 부분 일치를 살린다.
    빈 문자열이면 호출부가 키워드 축을 건너뛰면 된다.

    한국어는 **붙여 쓴 뒤 조사가 붙는다** - `서버 위치`로 찾으면 문서의 `서버별`은 다른
    lexeme이라 키워드 축이 통째로 헛돈다(`simple` 사전은 형태소 분석을 하지 않는다).
    그래서 각 토큰을 **접두 질의(`서버:*`)** 로 만든다: `서버별`·`서버들`·`서버의`가 다 걸린다.
    영어 토큰에도 같은 이득이 있다(`location` ← `locations`). 과잉 매칭은 RRF의 한 축일
    뿐이고 리랭커가 다시 거른다 - 반대로 **못 찾은 것은 되살릴 방법이 없다** (#180).
    """
    toks = [_UNSAFE_TS.sub("", t) for t in tokens_of(query)]
    toks = [t for t in toks if len(t) >= 2]
    return " | ".join(f"{t}:*" for t in toks)


def expand_query(query: str, max_variants: int = 3) -> list[str]:
    """검색에 쓸 질의 변형 목록(원문이 항상 첫 번째).

    LLM 호출 없이 만드는 가벼운 확장이다. 첫 번째는 원문 그대로라, 확장이 도움이 안 되는
    경우에도 원래 검색 품질을 해치지 않는다.
    """
    q = (query or "").strip()
    if not q:
        return []
    variants = [q]
    toks = tokens_of(q)
    stripped = " ".join(toks)
    if stripped and stripped.lower() != q.lower():
        variants.append(stripped)
    # 조사/어미를 뗀 '핵심어만' 버전(2글자 이상 명사형 위주)
    core = " ".join(t for t in toks if len(t) >= 2 and not t.isdigit())
    if core and core.lower() not in [v.lower() for v in variants]:
        variants.append(core)
    return variants[:max_variants]


_TRGM_CACHE: dict[str, bool] = {}

# 3-gram 축 임계값. word_similarity(질의, 문서) 기준이라 문서 길이에 휘둘리지 않는다.
DEFAULT_TRGM_MIN_SIM = 0.3


async def trgm_min_similarity() -> float:
    """3-gram 축에서 후보로 받아들일 최소 word_similarity(설정 키로 조정 가능).

    주의 - similarity()가 아니라 word_similarity()를 쓴다.
    similarity(문서, 질의)는 두 문자열 '전체'의 3-gram 자카드라, 문서가 길수록 값이 0으로
    수렴한다. 500자 청크 대 15자 질의면 질의의 3-gram이 전부 들어 있어도 0.04 수준이라
    기본 임계값 0.3(pg_trgm.similarity_threshold)을 절대 넘지 못한다 - 즉 `문서 % 질의`
    조건은 **항상 거짓**이고 3-gram 축이 통째로 죽어 있었다.
    word_similarity(질의, 문서)는 '문서 안에서 질의와 가장 잘 맞는 구간'을 보므로
    문서 길이와 무관하게 의미 있는 값이 나온다(같은 예에서 0.37).
    """
    from config_store import get_config
    try:
        return float(await get_config("trgm_min_similarity", str(DEFAULT_TRGM_MIN_SIM)))
    except (TypeError, ValueError):
        return DEFAULT_TRGM_MIN_SIM


async def has_trgm(pool, key: str) -> bool:
    """pg_trgm 확장이 설치돼 있는지(1회 확인 후 캐시).

    마이그레이션이 아직 안 돌았거나 확장 설치가 실패한 환경에서도 검색이 죽지 않도록,
    없으면 3-gram 축 없이 기존 방식으로 동작한다.
    """
    if key in _TRGM_CACHE:
        return _TRGM_CACHE[key]
    try:
        found = await pool.fetchval("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        _TRGM_CACHE[key] = bool(found)
    except Exception as e:  # noqa: BLE001
        print(f"[retrieval] pg_trgm 확인 실패, 3-gram 축 없이 진행: {type(e).__name__}: {e}")
        _TRGM_CACHE[key] = False
    return _TRGM_CACHE[key]


def _trigrams(text: str) -> set[str]:
    t = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return {t[i:i + 3] for i in range(max(0, len(t) - 2))} or {t}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def log_stages(tag: str, query: str, candidates: int, ranked: int, final: int) -> None:
    """검색이 **어느 단계에서** 0건이 됐는지 남긴다 (#162).

    지금까지 로그는 `선검색 매뉴얼 0건 · VOC 0건` 한 줄뿐이라, 세 가지가 구별되지 않았다:
      · DB에 후보가 아예 없었다(질의가 안 맞거나 데이터가 없다)
      · 후보는 많았는데 **리랭커 임계값(`rerank_min_score`)이 전부 걷어냈다**
      · 리랭크까지 됐는데 중복 제거가 줄였다
    셋은 고쳐야 할 곳이 완전히 다르다. 못 가르면 계속 엉뚱한 데를 고치게 된다(#146·#149).
    """
    print(f"[{tag}] 후보 {candidates} → 리랭크 {ranked} → 최종 {final} · q={query[:70]!r}")


def mmr_dedup(items: list, text_of, limit: int, threshold: float = 0.85) -> list:
    """이미 고른 것과 거의 같은 항목을 걸러낸다(MMR의 다양성 항만 단순화해 적용).

    상위 결과가 사실상 같은 문장으로 채워지면 컨텍스트만 낭비하고 답에 보탬이 없다.
    입력은 이미 관련도 순으로 정렬돼 있다고 보고, 앞에서부터 훑으며 비슷한 것만 버린다.
    threshold=1이면 아무것도 버리지 않는다.
    """
    if threshold >= 1:
        return items[:limit]
    picked, sigs = [], []
    for item in items:
        sig = _trigrams(text_of(item))
        if any(_jaccard(sig, s) >= threshold for s in sigs):
            continue
        picked.append(item)
        sigs.append(sig)
        if len(picked) >= limit:
            break
    return picked
