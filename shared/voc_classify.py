"""
VOC 처리 주체 분류 — "이 답변의 조치를 **사용자가 자기 권한으로 재현할 수 있는가**" (#157).

## 왜 새로 만드나

예전에는 답변 텍스트에 특정 표현(`확인 결과`, `재기동`, `담당자가` …)이 있는지 보고 갈랐다.
사용자 지적: **"키워드 사용에 한계가 있더라고."** 맞다. 표현은 무한하고, 목록은 늘려도 계속
뚫린다(#145에서 지시문에 대해 배운 것과 같은 실패다). 목록을 늘리는 대신 **판별 기준을 주고
모델이 적용하게 한다.**

## 적용한 것

1. **기준(rubric) 기반.** 표현이 아니라 *권한*을 묻는다 — "이 조치를 하려면 운영자 권한이나
   사용자에게 없는 접근이 필요한가?" 답변이 어떤 낱말을 쓰든 이 질문의 답은 같다.
2. **근거를 라벨보다 먼저 쓰게 한다.** JSON 스키마의 필드 순서가 `reason` → `label`이다.
   토큰은 왼쪽에서 오른쪽으로 생성되므로, 라벨은 자기가 방금 쓴 근거를 **조건으로** 나온다.
   순서를 뒤집으면 근거가 라벨을 정당화하는 사후 설명이 되어 정확도가 떨어진다.
3. **구조화 출력(guided decoding).** `response_format`으로 JSON 스키마를 강제한다. vLLM이
   문법 제약으로 디코딩하므로 파싱 실패·라벨 오타가 원천적으로 없다. 서버가 이 기능을 거부하면
   자동으로 평문 JSON 모드로 내려가고, 그때만 파서가 일한다.
4. **기권(abstain)을 준다.** `unknown`이 있어야 애매한 건을 찍지 않는다. 지어내는 것보다
   "모르겠다"가 낫다는 원칙은 여기서도 같다. `unknown`은 보수적으로 다뤄진다(운영자 건과 동일).
5. **경계를 가르치는 대조 예시**만 둔다. 낱말 목록이 아니라 *왜* 그쪽인지를 보여 준다.
6. **온도 0 + id 에코 검증.** 배치로 묶어 보내되 모델이 id를 되돌려주게 하고, 우리가 보낸
   id가 아니거나 빠진 건은 **버린다**(미분류로 남겨 다음 회차에 다시 시도).

## 라벨

| 값 | 뜻 | 에이전트가 할 일 |
|---|---|---|
| `user` | 사용자가 자기 계정 권한으로 그대로 할 수 있는 조치 | 방법을 안내해도 된다 |
| `operator` | 운영자 권한·접근이 있어야 하는 조치 | 그 조치를 시키지 않는다(매뉴얼 안내 후 접수) |
| `unknown` | 답변만으로는 못 가른다 | `operator`와 같이 다룬다 |

부수효과 없음: import만으로는 DB에도 LLM에도 붙지 않는다.
"""
import asyncio
import json
import re

import httpx

from config_store import get_config

LABELS = ("user", "operator", "unknown")

# 답변이 이 길이보다 길면 앞부분만 보낸다. 판정에 필요한 것은 '무엇을 했는가'이고
# 그건 거의 항상 앞쪽에 있다. 뒤에 붙은 로그 덤프까지 보내면 배치가 컨텍스트를 넘긴다.
MAX_ANSWER_CHARS = 1200
MAX_QUESTION_CHARS = 400

_RUBRIC = """당신은 사내 인프라 운영팀의 과거 문의 기록을 분류합니다.

# 판정할 것 — 단 하나입니다

각 기록의 **답변에 적힌 조치를, 문의한 그 사용자가 자기 계정 권한으로 그대로 다시 할 수
있는가?**

- 할 수 있다 → `user`
- 운영자 권한이나 사용자에게 없는 접근(서버 로그, 장비 콘솔, 계정·쿼터 변경 권한, 타인 자원)이
  있어야 한다 → `operator`
- 답변에 실제 조치 내용이 없어서 어느 쪽인지 가릴 수 없다 → `unknown`

# 판정 방법

낱말로 판단하지 마세요. 답변에 어떤 표현이 쓰였는지가 아니라, **거기 적힌 일을 하려면 누구의
권한이 필요한지**를 보세요. 같은 일을 두고도 사람마다 다르게 씁니다.

스스로 이렇게 물어보세요: "이 사용자가 지금 자기 터미널 앞에 앉아서, 이 답변만 보고 똑같이
따라 할 수 있는가?"

- 따라 할 수 있으면 `user`입니다. 답변을 운영자가 썼다는 사실은 상관없습니다 — 모든 답변은
  운영자가 씁니다. 중요한 것은 **조치의 주체**입니다.
- 따라 할 수 없으면 `operator`입니다. "이미 처리해 두었습니다"처럼 **결과만 통보**하는 답변도
  여기에 속합니다. 사용자가 재현할 방법이 답변에 없기 때문입니다.
- 안내·사과·접수 확인만 있고 조치가 없으면 `unknown`입니다. 억지로 한쪽으로 정하지 마세요.

# 경계 사례 — 왜 그쪽인지 보세요

- "제출 스크립트의 메모리 옵션을 늘려서 다시 제출해 보세요" → `user`
  (사용자 본인의 스크립트, 본인 권한으로 가능)
- "노드 상태를 보니 디스크가 가득 차 있어 정리했습니다. 지금 다시 시도해 보세요" → `operator`
  (노드 점검·정리는 운영자만 가능. 뒤의 '다시 시도'는 조치가 아니라 확인 요청)
- "쿼터를 500GB로 늘렸습니다" → `operator` (권한 변경)
- "홈 디렉토리의 불필요한 파일을 정리하시면 됩니다" → `user` (본인 파일)
- "확인 결과 정상입니다" → `unknown` (조치가 없다)
- "담당자에게 전달했습니다" → `unknown` (조치 내용이 없다)

# 출력

각 기록마다 `id`, `reason`, `label`을 냅니다.
**`reason`을 먼저 쓰고 그 다음에 `label`을 쓰세요** — 판단한 뒤에 이름을 붙이는 순서입니다.
`reason`은 한국어 한 문장(40자 이내)으로, **누구의 권한이 필요한지**를 적습니다.
받은 기록 전부에 대해 하나씩 내고, 받지 않은 id는 만들지 마세요."""

# 구조화 출력 스키마. 필드 순서가 곧 생성 순서다 — reason이 label보다 **먼저**여야 한다.
_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "integer"},
                    "reason": {"type": "string", "maxLength": 120},
                    "label": {"type": "string", "enum": list(LABELS)},
                },
                "required": ["id", "reason", "label"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["results"],
    "additionalProperties": False,
}


def _clip(text: str | None, limit: int) -> str:
    t = (text or "").strip()
    return t[:limit] + " …(생략)" if len(t) > limit else t


def _payload(records: list[dict]) -> str:
    lines = []
    for r in records:
        lines.append(json.dumps({
            "id": r["id"],
            "question": _clip(r.get("question"), MAX_QUESTION_CHARS),
            "answer": _clip(r.get("answer"), MAX_ANSWER_CHARS),
        }, ensure_ascii=False))
    return "\n".join(lines)


def parse_response(text: str) -> list[dict]:
    """모델 응답에서 결과 배열을 꺼낸다.

    구조화 출력이 먹으면 이 함수는 그냥 `json.loads`다. 서버가 스키마를 거부해 평문으로
    떨어졌을 때를 위해 세 가지를 견딘다: 추론형 모델의 `<think>` 블록, ```json 펜스,
    앞뒤에 붙은 설명 문장.
    """
    t = re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.M).strip()
    if not t:
        return []
    try:
        data = json.loads(t)
    except json.JSONDecodeError:
        # 앞뒤에 설명이 붙은 경우: 가장 바깥 중괄호만 떼어 다시 시도한다.
        i, j = t.find("{"), t.rfind("}")
        if i < 0 or j <= i:
            return []
        try:
            data = json.loads(t[i:j + 1])
        except json.JSONDecodeError:
            return []
    if isinstance(data, list):
        return data
    return data.get("results") or [] if isinstance(data, dict) else []


def validate(results: list, sent_ids: set) -> dict[int, tuple[str, str]]:
    """모델이 준 것 중 **우리가 보낸 id에 대한, 아는 라벨만** 받는다.

    배치 분류가 조용히 어긋나는 전형적인 사고가 두 가지다 — 모델이 id를 지어내거나(없는 행에
    라벨이 붙는다), 순서를 밀어서 답한다(전부 한 칸씩 틀린다). id를 되돌려받아 대조하면 둘 다
    걸린다. 걸린 건은 **버리고 미분류로 남긴다** — 다음 회차에 다시 시도하면 된다.
    """
    out: dict[int, tuple[str, str]] = {}
    for item in results or []:
        if not isinstance(item, dict):
            continue
        try:
            rid = int(item.get("id"))
        except (TypeError, ValueError):
            continue
        label = str(item.get("label") or "").strip().lower()
        if rid not in sent_ids or label not in LABELS or rid in out:
            continue
        out[rid] = (label, _clip(item.get("reason"), 120))
    return out


async def classify_records(records: list[dict], *, timeout: float = 120.0
                           ) -> dict[int, tuple[str, str]]:
    """{id: (label, reason)}. 실패하거나 검증에서 걸린 건은 **빠진다**(미분류 유지)."""
    records = [r for r in records if (r.get("answer") or "").strip()]
    if not records:
        return {}
    base = await get_config("vllm_llm_base_url")
    model = await get_config("vllm_llm_model", "qwen3-32b")
    if not base:
        raise RuntimeError("vllm_llm_base_url이 설정되어 있지 않습니다.")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _RUBRIC},
            {"role": "user", "content": _payload(records)},
        ],
        # 분류는 창의성이 필요 없다. 같은 입력에 같은 라벨이 나와야 재분류가 의미를 갖는다.
        "temperature": 0,
        "max_tokens": 120 * len(records) + 256,
        # 문법 제약 디코딩. 스키마를 벗어난 토큰이 아예 생성되지 않는다.
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "voc_handling", "schema": _SCHEMA, "strict": True},
        },
        # Qwen3의 사고 블록은 끈다. 근거는 스키마의 reason 필드로 이미 받고 있고,
        # 사고 블록까지 켜면 배치마다 토큰이 몇 배로 든다.
        "chat_template_kwargs": {"enable_thinking": False},
    }

    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(f"{base.rstrip('/')}/chat/completions", json=body)
        if resp.status_code >= 400:
            # 구조화 출력이나 chat_template_kwargs를 지원하지 않는 서버(구버전 vLLM, TGI 등).
            # 스키마 없이 한 번 더 시도한다 - 이때는 parse_response가 일한다.
            print(f"[voc-classify] 구조화 출력 거부({resp.status_code}), 평문 JSON으로 재시도")
            body.pop("response_format", None)
            body.pop("chat_template_kwargs", None)
            body["messages"][0]["content"] += (
                "\n\n반드시 {\"results\": [...]} 형태의 JSON만 출력하세요. 설명을 붙이지 마세요.")
            resp = await client.post(f"{base.rstrip('/')}/chat/completions", json=body)
        resp.raise_for_status()
        text = ((resp.json().get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""

    return validate(parse_response(text), {r["id"] for r in records})


async def _batch_size() -> int:
    try:
        return max(1, int(await get_config("voc_classify_batch", "12")))
    except (TypeError, ValueError):
        return 12


async def _concurrency() -> int:
    try:
        return max(1, int(await get_config("voc_classify_concurrency", "4")))
    except (TypeError, ValueError):
        return 4


_UPDATE_SQL = """
UPDATE voc_records
SET handled_by = $2, handled_by_reason = $3,
    handled_by_source = 'llm', handled_by_at = now()
WHERE id = $1
"""


async def classify_pending(pool, limit: int = 0, *, progress=None,
                           should_stop=None) -> dict:
    """`handled_by`가 비어 있는 행을 찾아 분류하고 저장한다.

    배치 하나가 실패해도 나머지는 계속 간다 — 수천 건짜리 백필이 한 건 때문에 통째로
    죽으면 안 된다(VOC 일괄 등록에서 같은 사고가 있었다: 333건 중 16건).
    실패한 배치의 행은 미분류로 남아 다음 실행 때 다시 시도된다.

    **배치를 동시에 보낸다** (#160). 예전에는 한 번에 하나씩 `await` 했다 — 배치 하나가
    끝나야 다음 요청이 나갔으므로, LLM이 생성하는 동안 말고는 GPU가 놀았다. vLLM은 여러
    요청을 겹쳐 처리하도록(continuous batching) 만들어진 서버라 이건 그냥 낭비다.
    수천 건 백필이 몇 시간 걸리던 이유가 여기에 있다.

    동시 요청 수는 `voc_classify_concurrency`(기본 4)로 정한다. 무한정 올리지 않는 이유는
    **같은 vLLM을 사용자 채팅이 쓰고 있기 때문**이다 — 분류가 큐를 다 차지하면 그동안
    답변이 느려진다.

    `should_stop()`이 참을 돌려주면 **남은 배치를 시작하지 않는다**(#161). 태스크를 강제로
    취소하지 않는 이유: 이미 LLM에 나가 있는 배치는 응답이 오는 중이고, 그걸 중간에 끊으면
    그 판정은 그냥 버려진다. 도는 것은 끝내서 저장하고 대기 중인 것만 접으면, 사용자가
    '중지'를 눌러도 **한 건도 잃지 않는다.**
    """
    rows = await pool.fetch(
        """
        SELECT id, question, answer FROM voc_records
        WHERE handled_by IS NULL AND answer IS NOT NULL AND answer <> ''
        ORDER BY id
        """ + (" LIMIT $1" if limit and limit > 0 else ""),
        *([limit] if limit and limit > 0 else []),
    )
    total = len(rows)
    if not total:
        return {"total": 0, "classified": 0, "failed": 0, "counts": {}}

    size = await _batch_size()
    chunks = [[dict(r) for r in rows[s:s + size]] for s in range(0, total, size)]
    gate = asyncio.Semaphore(await _concurrency())
    stat = {"done": 0, "classified": 0, "failed": 0, "stopped": 0}
    counts = {k: 0 for k in LABELS}

    async def run(chunk: list[dict]):
        async with gate:
            # 세마포어를 잡은 **직후**에 본다. 여기서 걸러야 대기 중이던 배치가 LLM으로
            # 나가지 않는다(순서를 바꿔 앞에서 보면 이미 gather에 다 들어와 있어 소용없다).
            if should_stop is not None and should_stop():
                stat["stopped"] += len(chunk)
                return
            try:
                verdicts = await classify_records(chunk)
            except Exception as e:  # noqa: BLE001
                print(f"[voc-classify] 배치 실패(건너뜀): {type(e).__name__}: {e}")
                verdicts = {}
            if verdicts:
                # 한 배치의 갱신을 한 번에 보낸다. 행마다 왕복하면 배치당 왕복이 12번이다.
                await pool.executemany(
                    _UPDATE_SQL,
                    [(rid, label, reason) for rid, (label, reason) in verdicts.items()])
                for label, _reason in verdicts.values():
                    counts[label] += 1
            # 여기서는 await 하지 않는다 — 이벤트 루프가 하나라 카운터 갱신은 원자적이다.
            stat["classified"] += len(verdicts)
            stat["failed"] += len(chunk) - len(verdicts)
            stat["done"] += len(chunk)
            if progress:
                progress(stat["done"], total, stat["classified"], stat["failed"])

    await asyncio.gather(*(run(c) for c in chunks))
    if stat["stopped"]:
        print(f"[voc-classify] 중지: {stat['stopped']}건을 시작하지 않았습니다(미분류로 남음)")
    return {"total": total, "classified": stat["classified"], "failed": stat["failed"],
            "stopped": stat["stopped"], "counts": counts}
