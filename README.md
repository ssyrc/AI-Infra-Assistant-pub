# AI Infra Assistant

사내 인프라·시스템 운영 문의를 받아 **매뉴얼과 과거 문의(VOC)에서 근거를 찾고, 필요하면
사용자 본인 권한으로 커맨드를 실행해** 한국어로 답하는 에이전트. 폐쇄망 전용, 외부 API 호출
없음, LLM·임베딩·리랭커 전부 사내 vLLM.

---

## 1. 전체 구조 — 요청 하나가 처리되는 과정

```mermaid
%%{init: {'flowchart': {'curve': 'linear'}}}%%
flowchart TB
    OW["Open WebUI<br/>사용자 채팅"]
    SH["Service Hub<br/>사내 VOC 시스템"]

    subgraph AG["agent-server · FastAPI + Google ADK"]
        direction TB
        API["OpenAI 호환 API<br/>/v1/chat/completions · /v1/voc/query"]
        PRE["질문 계획 · 선검색<br/>실행/검색 분기 · 질의 재작성"]
        RUN["ADK Runner + Agent<br/>도구 호출 루프 · 세션 · SSE 스트리밍"]
        LL["LiteLlm<br/>ADK genai 타입 ↔ OpenAI 타입 번역"]
        POST["근거 검사 · 후처리<br/>미근거 값 제거 · 차트 인라인 · 답변 출력"]
        API --> PRE --> RUN --> POST
        RUN --> LL
    end

    subgraph MCPS["MCP 4종 · FastMCP · streamable HTTP"]
        direction LR
        M1["Manual<br/>매뉴얼 RAG"]
        M2["VOC<br/>과거 문의 RAG"]
        M3["Execution<br/>커맨드 실행"]
        M4["Chart<br/>SVG 차트"]
    end

    VLLM["vLLM · OpenAI 호환 서버<br/>Qwen3-235B-A22B<br/>bge-m3 · bge-reranker-v2-m3"]
    HOST["게이트/로그인 서버<br/>ssh · 본인 계정으로 강등"]
    PG[("PostgreSQL + pgvector<br/>설정 · 청크 · 임베딩 · 세션 · 이력")]
    CON["관리자 콘솔<br/>설정 · 매뉴얼 · 커맨드 등록"]

    OW -->|"OpenAI 호환 HTTP"| API
    SH --> API
    PRE --> VLLM
    LL -->|"OpenAI 호환 HTTP"| VLLM
    RUN -->|"tool call · streamable HTTP"| MCPS
    MCPS --> PG
    M3 --> HOST
    CON --> PG
    PG -.->|"설정값"| AG
```

**agent-server 안의 세 겹을 구분해서 보면 된다.**

- **Google ADK** — 에이전트를 *정의하고 굴리는* 층. `Agent`가 모델·지시문·툴(MCP 4종)을 묶고,
  `Runner`가 도구 호출 루프를 돌리며, `DatabaseSessionService`가 대화 세션을 들고,
  `RunConfig(SSE)`가 토큰 스트리밍을 켠다. **MCP는 ADK가 `McpToolset`으로 직접 붙는다**
  (LiteLLM을 거치지 않는다).
- **LiteLlm** — ADK와 LLM 사이의 *요청/응답 담당*. 아래 참조.
- **우리 코드** — 질문 계획·선검색·근거 검사·후처리. 여기서 부르는 LLM/임베딩은 ADK를 타지 않고
  `httpx`로 vLLM을 직접 친다.

### vLLM에 그냥 붙이면 안 되나 — 안 된다

**ADK에는 OpenAI 호환 클라이언트가 없다.** `google/adk/models/`가 자체로 가진 모델 클래스는
`Gemini`(google-genai) · `Gemma` · `ApigeeLlm` · `Claude`(anthropic SDK)뿐이고,
그 외 모든 백엔드는 `LiteLlm`을 통해 붙게 되어 있다. vLLM은 OpenAI 호환 API로 서빙되므로
ADK가 말을 걸 수 있는 경로가 `LiteLlm` 하나다(대안은 `BaseLlm`을 직접 구현하는 것뿐).

게다가 단순 HTTP 중계가 아니다. **ADK 내부는 google.genai 타입**(`types.Content`,
`types.FunctionDeclaration`), **vLLM은 OpenAI 타입**(`messages[]`, `tools[]`, `tool_calls[]`)이라
양방향 번역이 필요하다 — MCP 툴 스키마를 OpenAI `tools[]`로 바꾸고, 돌아온 `tool_calls`를 다시
ADK 이벤트로 되돌리는 일. 툴을 쓰는 에이전트에서는 이 번역이 핵심 경로다.

```
Open WebUI / Service Hub ──OpenAI 호환 HTTP──▶ agent-server (FastAPI)   ← 우리가 서빙하는 쪽
                                                    │
                                            ADK Runner + Agent ──streamable HTTP──▶ MCP 4종
                                                    │  (google.genai 타입)
                                                 LiteLlm                 ← 번역기
                                                    │  ──OpenAI 호환 HTTP──▶ vLLM
```

양 끝에 "OpenAI 호환"이 두 번 나오지만 서로 다른 것이다. 입구는 Open WebUI가 그 스펙만 말할 줄
알아서 우리가 흉내 낸 것이고, 출구가 LiteLlm이 맡는 부분이다.

| 계층 | 기술 | 이 구조에서 맡는 것 |
|---|---|---|
| 에이전트 런타임 | **Google ADK** `Runner`·`Agent`·`McpToolset` | 도구 호출 루프, MCP 연결, 세션, SSE 스트리밍 |
| 모델 접속 | **LiteLlm** (`openai/…`) | ADK↔OpenAI 타입 번역, 사내 vLLM 호출 |
| API | **FastAPI** | OpenAI 호환 엔드포인트, Service Hub 위임 API |
| 도구 | **FastMCP** · streamable HTTP | MCP 4종. 호출자 헤더 전달 |
| 저장 | **PostgreSQL + pgvector** · asyncpg | 청크·임베딩·설정·세션·이력 |
| 화면 | **Open WebUI** / React + Babel standalone | 사용자 채팅 / 관리자 콘솔 |

**관리자 콘솔은 요청 경로에 없다.** 모델 주소·모델명·시스템 지시문·등록 커맨드·API 키를 DB에
써 넣고, agent-server와 MCP가 매 요청 그 값을 읽는다(위 그림의 점선).

---

## 2. MCP별 실행 구조

```mermaid
flowchart TB
    subgraph MAN["Manual MCP · 매뉴얼 RAG"]
        direction TB
        MQ["질의 · 멀티 쿼리"] --> M3X["3축 병렬<br/>벡터 cosine · tsvector 접두 질의 · pg_trgm"]
        M3X --> MRRF["RRF 융합 1/(60+rank)"] --> MMERGE["질의별 후보 병합"]
        MMERGE --> MRR["cross-encoder 리랭킹<br/>원 질문 기준 1회 · 관련도 하한"]
        MRR --> MMMR["MMR 중복 제거"] --> MNB["이웃 청크 확장<br/>예산 우선 배치"]
        MNB --> MPII["계정·이메일 마스킹"]
    end

    subgraph VOC["VOC MCP · 과거 사례 RAG"]
        direction TB
        VQ["질의 · 멀티 쿼리"] --> V3X["3축 병렬 + RRF"]
        V3X --> VRR["리랭킹 · MMR"]
        VRR --> VH["처리 주체 판정<br/>user / operator / unknown"]
        VH --> VPII["계정·이름·조직 전체 마스킹"]
    end

    subgraph EXE["Execution MCP · 커맨드 실행"]
        direction TB
        ET["도구 노출<br/>등록 커맨드 = 전용 도구<br/>그 밖 = run_command"] --> EV["인자 검증<br/>타입·선택지·개수·길이"]
        EV --> ED["차단 목록 · 타 계정 지목 차단"]
        ED --> EA["argv 리스트 구성 · 셸 미경유"]
        EA --> ESSH["ssh → 즉시 권한 강등"]
        ESSH --> ER["exit_code · stdout · duration_ms<br/>실행 이력 기록"]
    end

    subgraph CHT["Chart MCP · 시각화"]
        direction TB
        CD["실행·조회로 얻은 수치만"] --> CS["SVG 생성<br/>8종 · 값 라벨 · 범례"]
        CS --> CM["표시자 chart://id 반환<br/>응답 직전 data URI 치환"]
    end

    MPII & VPII --> P["프롬프트 근거 블록"]
    ER --> P2["실행 결과 · 원문 병기"]
    CM --> P3["답변 내 인라인 이미지"]
```

| MCP | 입력 | 출력 | 특징 |
|---|---|---|---|
| **Manual** | 자연어 질의 | 근거 문단 + 문서 위치 | 읽기 전용. 문서 안내 문구를 그대로 옮길 수 있게 위치·문서명 분리 제공 |
| **VOC** | 자연어 질의 | 과거 문의/답변 + 처리 주체 | 읽기 전용. 처리 주체에 따라 *방법 안내* 와 *원인 추측* 분기 |
| **Execution** | 도구별 인자 | 실행 결과 원문 | 유일한 쓰기 경로. 6단계 검증 후 본인 계정 실행 |
| **Chart** | 수치 배열 | SVG 표시자 | 실행·DB 조회 없음. 프롬프트 예산 보호를 위해 표시자만 반환 |

---

## 3. 보안 — 커맨드는 **본인 권한으로만** 실행

```mermaid
flowchart LR
    Q["질문"] --> A["agent-server<br/>API 키 · X-User-Id"]
    A --> G1["① MCP 인증<br/>공유 비밀값"]
    G1 --> G2["② 신원 고정<br/>user_id 주입 · 역할 검사"]
    G2 --> G3["③ 인자 검증<br/>스키마 · 타 계정 지목 차단"]
    G3 --> G4["④ 커맨드 검증<br/>차단 목록 전 토큰 · 셸 미경유"]
    G4 --> E["⑤ ssh root → 즉시 권한 강등<br/>본인 계정으로 실행"]
    E --> L["⑥ 이력 기록 · 근거 검사"]
```

**어떤 질문이 어디서 막히는가**

| 질문 | 결과 |
|---|---|
| `{남의 계정} job 현황 보여줘` | ③에서 `-u 남의계정` 인자 거부. 답변에서도 타 계정이 든 줄 제거 후 "확인 불가" 안내 |
| `다른 사람이 올린 VOC 알려줘` | 검색 결과가 MCP 반환 이전에 마스킹 → 프롬프트에 원문 미유입 |
| `홈 디렉토리 전부 지워줘` | ④에서 `rm` 거부. `mpirun … rm -rf /`처럼 뒤에 숨겨도 전 토큰 검사로 동일 거부 |
| `sudo로 서비스 재시작해줘` | ④에서 `sudo`·`systemctl` 거부 |
| `bash -c "…"` / `ssh 다른서버 …` | ④에서 실행 위임 커맨드 거부. 열어 두면 위 차단이 전부 무의미 |
| `cat 파일 \| grep 키워드` | 셸 미경유라 파이프가 글자 그대로 전달 → 등록·실행 단계에서 거부 |
| 근거 없는 `서버 IP·경로` 안내 | ⑥ 근거 검사가 그 줄만 제거. 남는 내용이 없으면 운영팀 안내로 대체 |
| 본인 계정 실행 결과 | **마스킹 없음.** 실행은 항상 본인 권한이므로 결과 전체가 본인 정보 |

`ssh root@host`로 접속하되 **직후 항상** 호출자 계정으로 강등(`su-login` / `su` / `runuser`).
우회 경로 미제공. 내부 포트(postgres·MCP 4종)는 `127.0.0.1`에만 개방.

---

## 4. 관리자 콘솔

| 탭 | 관리 대상 |
|---|---|
| **매뉴얼** | 문서 업로드(엑셀·PPT·워드) → 청크·임베딩 생성 → **발행** 시 검색 대상 편입. 문서 위치(메뉴 경로·URL) 등록, 검색 테스트로 단계별 건수 확인 |
| **VOC** | 과거 문의/답변 적재, 임베딩 생성, **처리 주체 분류**(먼저 30건 / 미분류 전부 / 중지) |
| **커맨드 실행** | 커맨드 등록·수정·활성/역할, 인자 명세(타입·설명·선택지), 엑셀 양식 일괄 등록·내보내기, 실행 이력 조회 |
| **설정** | 아래 파라미터 전부. 시스템 지시문 최신 기본값 복원, 서비스 재시작 |
| **계정** | 관리자 계정·역할 |

**설정 파라미터 구역**

| 구역 | 주요 항목 |
|---|---|
| 에이전트 | 시스템 지시문, API 키, 근거 검사 on/off, 선검색 강제, 질문 계획 on/off, 온도, 스트리밍, 이력 예산, 진행 줄 표시 |
| LLM / 임베딩·리랭커 | 각 서빙 주소·모델명, 임베딩 차원, 리랭커 제공자·관련도 하한 |
| Manual MCP | 선검색 건수, 결과 길이 상한, 이웃 청크 범위 |
| VOC MCP | 선검색 건수, 결과 길이 상한, 분류 배치·동시 실행 수, 접수 안내 문구 |
| Execution MCP | 실행 대상 호스트, 결과 길이 상한, 도구 개수 상한, **차단 커맨드 목록**, **타 계정 지목 옵션 목록**, 실행 원문 표시 |
| Chart MCP | 최대 점 개수, 이미지 보존 시간 |
| Open WebUI | 사용자 접속 주소, API 주소, 관리자 키 |

- `enabled` / `required_roles`는 즉시 반영. 등록 커맨드·인자·설명 변경은 `execution-mcp`
  재시작 시 도구 목록 재구성.
- 설정은 `platform_config`에 저장. 콘솔에서 저장한 값은 초기화 시드가 덮어쓰지 않음.

---

> **이 저장소는 공개용 사본입니다.**
> 서버 주소·계정·경로는 전부 자리표시자로 치환(`10.0.0.30`, `ops.user`, `/home/users` 등).
> 실제 값은 배포 환경의 `.env`와 설정 DB에만 존재.
> 기본 모드에서는 코드만 반출하므로 `docs/`·`CLAUDE.md`·사이트 전용 운영 스크립트는 미포함.
