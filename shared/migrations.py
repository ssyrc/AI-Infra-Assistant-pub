"""
DB 마이그레이션 + 설정 부트스트랩 러너.

해결하는 문제:
1) credential을 SQL 시드에 하드코딩하지 않는다. DB/Redis 접속 정보는 환경변수에서 읽어
   platform_settings에 주입하므로, POSTGRES_PASSWORD를 바꿔도 DSN이 자동으로 맞춰진다.
2) init-db/*.sql은 Postgres 최초 기동에만 실행되므로, 이후 추가되는 스키마 변경/신규 설정 키가
   기존 DB에 반영되지 않는다. 여기서 버전별 마이그레이션을 매 기동 시 멱등하게 적용한다.

실행: compose의 db-init 원샷 서비스가 다른 서비스보다 먼저 실행한다.
      python -m migrations  (또는 python migrations.py)
"""
import os
import json
import asyncio
import secrets

import asyncpg

from execution_exec import DEFAULT_DENY_CSV, DEFAULT_USER_SCOPE_CSV, tool_name_for
from agent_instruction import AGENT_INSTRUCTION

PG_HOST = os.environ.get("POSTGRES_HOST", "postgres")
PG_PORT = os.environ.get("POSTGRES_PORT", "5432")
PG_USER = os.environ.get("POSTGRES_USER", "agent")
PG_PASSWORD = os.environ["POSTGRES_PASSWORD"]

REDIS_HOST = os.environ.get("REDIS_HOST", "redis")
REDIS_PORT = os.environ.get("REDIS_PORT", "6379")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
REDIS_CACHE_DB = os.environ.get("REDIS_CACHE_DB", "1")

APP_DBS = ["platform_config", "manual_db", "voc_db", "command_db", "system_db",
           "agent_sessions_db", "memory_db", "langfuse"]


def dsn(db: str) -> str:
    return f"postgresql://{PG_USER}:{PG_PASSWORD}@{PG_HOST}:{PG_PORT}/{db}"


def redis_url() -> str:
    if not REDIS_HOST:
        return ""
    auth = f":{REDIS_PASSWORD}@" if REDIS_PASSWORD else ""
    return f"redis://{auth}{REDIS_HOST}:{REDIS_PORT}/{REDIS_CACHE_DB}"


# --- 버전별 마이그레이션 ---------------------------------------------------------
# (db, version, sql). 같은 (db, version)은 한 번만 적용된다.
# 새 변경은 반드시 새 version을 추가하는 방식으로만 넣는다(기존 항목 수정 금지).
MIGRATIONS: list[tuple[str, int, str]] = [
    ("platform_config", 1, """
        CREATE TABLE IF NOT EXISTS platform_settings (
            key          TEXT PRIMARY KEY,
            value        TEXT NOT NULL,
            description  TEXT,
            hot_reload   BOOLEAN NOT NULL DEFAULT false,
            is_secret    BOOLEAN NOT NULL DEFAULT false,
            updated_by   TEXT,
            updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # 관리자 콘솔 계정 관리(.env의 ADMIN_USER는 잠금 방지용 기본 계정으로 항상 별도 유효,
    # 여기 등록된 계정은 그 외 추가 관리자용). 비밀번호는 bcrypt 해시로만 저장한다.
    ("platform_config", 2, """
        CREATE TABLE IF NOT EXISTS admin_accounts (
            username      TEXT PRIMARY KEY,
            password_hash TEXT NOT NULL,
            created_by    TEXT,
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    ("manual_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS manual_files (
            id SERIAL PRIMARY KEY,
            title TEXT NOT NULL,
            filename TEXT NOT NULL,
            source_type TEXT NOT NULL DEFAULT 'document',
            uploaded_by TEXT,
            uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            published_at TIMESTAMPTZ,
            version INT NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'draft'
        );
        CREATE TABLE IF NOT EXISTS manual_chunks (
            id SERIAL PRIMARY KEY,
            manual_file_id INT REFERENCES manual_files(id) ON DELETE CASCADE,
            seq INT NOT NULL DEFAULT 0,
            section_title TEXT,
            page_no INT,
            chunk_text TEXT NOT NULL,
            embedding vector(1024),
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(chunk_text, ''))) STORED
        );
        CREATE INDEX IF NOT EXISTS manual_chunks_embedding_idx ON manual_chunks USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
    """),
    ("voc_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS voc_records (
            id SERIAL PRIMARY KEY,
            question TEXT NOT NULL,
            answer TEXT NOT NULL,
            resolved BOOLEAN NOT NULL DEFAULT true,
            department TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            embedding vector(1024),
            tsv tsvector GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(question, '') || ' ' || coalesce(answer, ''))
            ) STORED
        );
        CREATE INDEX IF NOT EXISTS voc_records_embedding_idx ON voc_records USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS voc_records_tsv_idx ON voc_records USING gin (tsv);
    """),
    ("command_db", 1, """
        CREATE TABLE IF NOT EXISTS command_catalog (
            id SERIAL PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT NOT NULL,
            usage TEXT,
            category TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    ("system_db", 1, """
        CREATE TABLE IF NOT EXISTS system_whitelist_state (
            tool_name TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT true,
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS job_logs (
            id SERIAL PRIMARY KEY,
            tool_name TEXT NOT NULL,
            params JSONB,
            requested_by TEXT,
            status TEXT,
            result JSONB,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # v2: 업로드 세션을 서버가 관리 (클라이언트가 경로/옵션을 결정하지 못하게)
    ("manual_db", 2, """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            saved_path  TEXT NOT NULL,
            kind        TEXT NOT NULL,          -- document | spreadsheet
            options     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # v3: 로그인 서버를 '이름'에서 'IP'로 강제 교체.
    #     배포 호스트 /etc/hosts에서 login-01이 게이트(10.0.0.100)가 아니라 10.0.1.7로
    #     풀리고 있었고, 그 서버엔 우리 키가 없어 커맨드 실행이 전부 인증 실패했다.
    #     이미 IP가 들어 있으면(=운영자가 의도적으로 정한 값) 건드리지 않는다.
    ("platform_config", 3, """
        UPDATE platform_settings
           SET value = '10.0.0.100', updated_at = now()
         WHERE key = 'scheduler_login_host'
           AND value !~ '^[0-9]{1,3}(\\.[0-9]{1,3}){3}$';
    """),
    # v4: scheduler_job_command 설정 키 제거.
    #     스케줄러 커맨드를 설정값으로 둔 것 자체가 잘못이었다 - 커맨드는 카탈로그(커맨드 탭)에
    #     등록하고 에이전트가 검색해서 실행해야 한다. 설정 탭에 커맨드가 하나 더 생기면
    #     "어디를 고쳐야 반영되나"가 두 곳이 되어 오히려 헷갈린다.
    ("platform_config", 4, """
        DELETE FROM platform_settings WHERE key = 'scheduler_job_command';
    """),
    # v5: Command MCP + System MCP -> Execution MCP 통합(#111). 새 키는 seed_config가 넣는다.
    #   관리자가 바꿔 둔 값은 옮겨 준다 - 통합했다고 설정을 다시 하게 만들지 않는다.
    #   *_mcp_url은 옮기지 않는다(컨테이너 이름·포트가 바뀌었으므로 새 기본값이 맞다).
    ("platform_config", 5, """
        INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
        SELECT 'execution_tools_max', value, '등록 커맨드를 MCP 툴로 노출할 최대 개수',
               false, false, updated_by
        FROM platform_settings WHERE key = 'command_tools_max'
        ON CONFLICT (key) DO NOTHING;

        INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
        SELECT 'execution_deny_commands', value, '실행을 거부할 명령 이름(콤마 구분)',
               true, false, updated_by
        FROM platform_settings WHERE key = 'catalog_exec_deny_commands'
        ON CONFLICT (key) DO NOTHING;

        DELETE FROM platform_settings
        WHERE key IN ('command_tools_max', 'catalog_exec_deny_commands',
                      'command_mcp_url', 'system_mcp_url', 'command_db_dsn');
    """),
    # v6: scheduler_login_host -> execution_host 개명(#128).
    #   'scheduler'라는 이름 탓에 "스케줄러 전용 설정"으로 읽혔는데, 실제로는 **모든 커맨드가
    #   실행되는 로그인 서버 주소**다. 관리자가 여기를 안 보고 지나쳐 실행이 통째로 죽는 일이
    #   반복됐다. 값은 그대로 옮긴다 - 개명 때문에 ssh가 끊기면 안 된다.
    ("platform_config", 6, """
        INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
        SELECT 'execution_host', value,
               '커맨드를 실행할 서버 주소(로그인 서버). 이름 말고 IP로 적는다',
               true, false, updated_by
        FROM platform_settings WHERE key = 'scheduler_login_host'
        ON CONFLICT (key) DO NOTHING;

        DELETE FROM platform_settings WHERE key = 'scheduler_login_host';
    """),
    # v3: 감사로그에 사용자/대화 식별자 추가
    ("system_db", 3, """
        ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS conversation_id TEXT;
        ALTER TABLE job_logs ADD COLUMN IF NOT EXISTS request_id TEXT;
        CREATE INDEX IF NOT EXISTS job_logs_created_idx ON job_logs (created_at DESC);
    """),
    # v4: 임베딩 모델 메타데이터 (모델 변경 시 재임베딩 판단용)
    ("manual_db", 4, """
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS embed_dim INT;
    """),
    ("voc_db", 4, """
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS embed_dim INT;
    """),
    # v2: 커맨드 카탈로그를 의미 검색(임베딩+FTS 하이브리드) 대상으로 승격.
    #     사용자가 "완전 일치" 키워드가 아니라 설명형으로 물어도 적절한 커맨드를 찾게 한다.
    ("command_db", 2, """
        CREATE EXTENSION IF NOT EXISTS vector;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embedding vector(1024);
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embed_model TEXT;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS embed_dim INT;
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(name, '') || ' ' || coalesce(description, '') || ' ' || coalesce(usage, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS command_catalog_embedding_idx
            ON command_catalog USING hnsw (embedding vector_cosine_ops);
        CREATE INDEX IF NOT EXISTS command_catalog_tsv_idx
            ON command_catalog USING gin (tsv);
    """),
    # v3: 커맨드 탭도 엑셀 업로드 미리보기 세션을 사용한다(매뉴얼과 동일한 보안 모델).
    ("command_db", 3, """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            saved_path  TEXT NOT NULL,
            kind        TEXT NOT NULL,
            options     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS command_upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # v4: 화이트리스트 설명/권한을 관리자 콘솔에서 편집할 수 있게 오버라이드 컬럼 추가.
    #     required_roles는 실행 시점에 실시간 반영, description_override는 MCP 재시작 시 반영.
    ("system_db", 4, """
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS required_roles TEXT[] NOT NULL DEFAULT '{}';
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS description_override TEXT;
    """),
    # v4: 스케줄러 실행 툴이 System에서 Command로 이동. Command도 실행형 MCP가 되므로
    #     활성/역할/설명 오버라이드 상태 테이블과 감사로그를 둔다(System과 동일 구조).
    ("command_db", 4, """
        CREATE TABLE IF NOT EXISTS command_whitelist_state (
            tool_name TEXT PRIMARY KEY,
            enabled BOOLEAN NOT NULL DEFAULT true,
            required_roles TEXT[] NOT NULL DEFAULT '{}',
            description_override TEXT,
            updated_by TEXT,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE IF NOT EXISTS job_logs (
            id SERIAL PRIMARY KEY,
            tool_name TEXT NOT NULL,
            params JSONB,
            requested_by TEXT,
            status TEXT,
            result JSONB,
            conversation_id TEXT,
            request_id TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS command_job_logs_created_idx ON job_logs (created_at DESC);
    """),
    # 사용자별 장기 메모리(단일 user_id 키). 대화 턴 원장 + 증류된 장기기억 + 대화 상태.
    # 상위 agent(예: 통합 VOC)에서 오는 요청도 이 메모리를 공유한다.
    ("memory_db", 1, """
        CREATE EXTENSION IF NOT EXISTS vector;
        CREATE TABLE IF NOT EXISTS memory_turns (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            conversation_id TEXT,
            source TEXT,
            role TEXT NOT NULL,                 -- 'user' | 'assistant'
            content TEXT NOT NULL,
            embedding vector(1024),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX IF NOT EXISTS memory_turns_conv_idx ON memory_turns (conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS memory_turns_user_idx ON memory_turns (user_id, created_at);
        CREATE INDEX IF NOT EXISTS memory_turns_emb_idx ON memory_turns USING hnsw (embedding vector_cosine_ops);

        -- 여러 대화에서 증류된 사용자 장기기억(사실/선호/요약). user_id 단위로 공유.
        CREATE TABLE IF NOT EXISTS user_memory (
            id BIGSERIAL PRIMARY KEY,
            user_id TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'fact',  -- 'fact' | 'preference' | 'summary'
            content TEXT NOT NULL,
            embedding vector(1024),
            source TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at TIMESTAMPTZ
        );
        CREATE INDEX IF NOT EXISTS user_memory_user_idx ON user_memory (user_id);
        CREATE INDEX IF NOT EXISTS user_memory_emb_idx ON user_memory USING hnsw (embedding vector_cosine_ops);

        -- 대화별 요약 진행 상태(어디까지 요약해 승격했는지).
        CREATE TABLE IF NOT EXISTS conversation_state (
            conversation_id TEXT PRIMARY KEY,
            user_id TEXT NOT NULL,
            turn_count INT NOT NULL DEFAULT 0,
            summarized_upto BIGINT NOT NULL DEFAULT 0,   -- 이 memory_turns.id 이하까지 요약 완료
            last_summarized_at TIMESTAMPTZ,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # v2: 예전 요약기가 "사실/선호/맥락"을 뽑게 되어 있어서 **인프라 절차**가 장기기억에
    #     들어갔다("CPU 노드에서 스크래치는 …를 쓴다"). 그게 시스템 지시문에 주입돼
    #     "GPU에서 스크래치 사용법"에 CPU 답을 내는 사고가 났다(#122).
    #     자동 요약분(source='auto-summary')을 한 번 비운다. 요약기 프롬프트를 고쳤으니
    #     앞으로 쌓이는 것은 사용자 자신에 대한 정보뿐이다.
    #     사람이 직접 넣은 기억(source가 다른 것)은 건드리지 않는다.
    ("memory_db", 2, """
        DELETE FROM user_memory WHERE source = 'auto-summary';
        UPDATE conversation_state SET summarized_upto = 0;
    """),
    # 관리자 콘솔에서 코드 배포 없이 새 System MCP 화이트리스트 커맨드를 등록할 수 있게 한다.
    # argv_template의 "{param}" 토큰이 params에 정의된 파라미터로 치환된다(셸 미사용, argv 그대로 실행).
    # 항상 user_id로 scope(호출자 권한 강제)되고 host가 필수라 기존 화이트리스트 항목과 안전모델이 같다.
    ("system_db", 5, """
        CREATE TABLE IF NOT EXISTS system_custom_commands (
            tool_name      TEXT PRIMARY KEY,
            description    TEXT NOT NULL,
            argv_template  JSONB NOT NULL,
            params         JSONB NOT NULL DEFAULT '[]',
            required_roles TEXT[] NOT NULL DEFAULT '{}',
            enabled        BOOLEAN NOT NULL DEFAULT false,
            created_by     TEXT,
            created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_by     TEXT,
            updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """),
    # v6: host 파라미터를 "해당 서버 실행"(LLM이 서버명을 지정)과 "로그인 서버 실행"
    #     (게이트/로그인 서버로 고정, LLM 스키마에서 host 자체를 숨김) 중 하나로 분류.
    #     화이트리스트/커스텀 커맨드 둘 다 같은 개념이라 두 테이블에 동일하게 추가한다.
    #     스키마(LLM에 보이는 파라미터)에 영향을 주므로 변경 후 System MCP 재시작이 필요하다.
    ("system_db", 6, """
        ALTER TABLE system_whitelist_state
            ADD COLUMN IF NOT EXISTS host_mode TEXT NOT NULL DEFAULT 'target_server';
        ALTER TABLE system_whitelist_state
            ADD CONSTRAINT system_whitelist_state_host_mode_check
            CHECK (host_mode IN ('target_server', 'login_server'));
        ALTER TABLE system_custom_commands
            ADD COLUMN IF NOT EXISTS host_mode TEXT NOT NULL DEFAULT 'target_server';
        ALTER TABLE system_custom_commands
            ADD CONSTRAINT system_custom_commands_host_mode_check
            CHECK (host_mode IN ('target_server', 'login_server'));
    """),
    # v5: VOC 탭도 엑셀/CSV 열 매핑 업로드를 쓴다(형식 고정 대신 어떤 표든 받기 위함).
    ("voc_db", 5, """
        CREATE TABLE IF NOT EXISTS upload_sessions (
            upload_id   TEXT PRIMARY KEY,
            owner       TEXT NOT NULL,
            filename    TEXT NOT NULL,
            ext         TEXT NOT NULL,
            saved_path  TEXT NOT NULL,
            kind        TEXT NOT NULL,
            options     JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at  TIMESTAMPTZ NOT NULL
        );
        CREATE INDEX IF NOT EXISTS voc_upload_sessions_expires_idx ON upload_sessions (expires_at);
    """),
    # --- 한국어 검색 강화 -------------------------------------------------------
    # 'simple' tsvector는 공백 토큰화만 해서 한국어에서 매칭이 거의 안 된다("접근하려면" != "접근").
    # 형태소 분석기는 폐쇄망 오프라인 설치가 번거로우므로, Postgres 기본 contrib인 pg_trgm의
    # 문자 3-gram을 세 번째 검색 축으로 추가한다. 확장이 없는 환경에서도 죽지 않도록
    # 코드가 pg_extension을 먼저 확인하고 없으면 이 축을 건너뛴다.
    # 또한 tsvector에 섹션 제목을 포함시켜(Contextual retrieval) 제목 키워드로도 잡히게 한다.
    ("manual_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS manual_chunks_trgm_idx
            ON manual_chunks USING gin (chunk_text gin_trgm_ops);
        ALTER TABLE manual_chunks DROP COLUMN IF EXISTS tsv;
        ALTER TABLE manual_chunks ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(section_title, '') || ' ' || coalesce(chunk_text, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
    """),
    # v8: 처리 주체(사용자가 직접 한 건 / 운영자가 확인·조치한 건)를 **저장**한다.
    #     예전에는 검색할 때마다 답변 텍스트에서 키워드로 추론했는데(`확인 결과`, `재기동`…),
    #     그 표현을 안 쓴 답변을 통째로 놓쳤다. 목록을 늘리는 방식은 계속 뚫린다(#157).
    #     이제 LLM이 '사용자가 자기 권한으로 재현할 수 있는가'를 판정해 여기에 넣는다.
    #     NULL이면 아직 분류 전이라는 뜻이고, 그때만 예전 키워드 추론으로 떨어진다.
    ("voc_db", 8, """
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS handled_by TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS handled_by_reason TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS handled_by_source TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS handled_by_at TIMESTAMPTZ;
        -- 미분류 행만 훑는 백필용. WHERE 절이 붙은 부분 인덱스라 크기가 작다.
        CREATE INDEX IF NOT EXISTS voc_records_unclassified_idx
            ON voc_records (id) WHERE handled_by IS NULL;
    """),
    # v7: VOC를 '업로드 묶음' 단위로도 다룰 수 있게 한다.
    #     CSV 한 개를 올리면 수천 행이 개별 레코드로 들어가는데, 콘솔에서 낱개로만 보이면
    #     "방금 올린 그 파일"을 통째로 되돌릴 방법이 없었다. batch_id로 묶어 두면
    #     묶음 목록/묶음 삭제가 가능해진다. 기존 데이터는 batch_id가 NULL(=출처 미상)이다.
    ("voc_db", 7, """
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS batch_id TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS source_file TEXT;
        ALTER TABLE voc_records ADD COLUMN IF NOT EXISTS uploaded_by TEXT;
        CREATE INDEX IF NOT EXISTS voc_records_batch_idx ON voc_records (batch_id);
    """),
    ("voc_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS voc_records_trgm_idx
            ON voc_records USING gin ((question || ' ' || answer) gin_trgm_ops);
    """),
    ("command_db", 6, """
        CREATE EXTENSION IF NOT EXISTS pg_trgm;
        CREATE INDEX IF NOT EXISTS command_catalog_trgm_idx
            ON command_catalog USING gin ((name || ' ' || description) gin_trgm_ops);
    """),
    # v5: 카탈로그(매뉴얼 엑셀 업로드본)에 등록된 커맨드를 그대로 실행할 수 있게 한다.
    #     exec_command = 실제 실행할 커맨드 문자열(셸 없이 shlex 분해 후 argv로 실행).
    #     비어 있으면 name을 그대로 실행한다 -> 기존에 올린 카탈로그도 추가 작업 없이 실행 가능.
    ("command_db", 5, """
        ALTER TABLE command_catalog ADD COLUMN IF NOT EXISTS exec_command TEXT;
    """),
    # v7: Command MCP + System MCP -> **Execution MCP 하나**로 합친다(#111).
    #   등록 커맨드는 여기 한 테이블로 모인다(구 command_catalog + system_custom_commands).
    #   물리 DB는 command_db를 그대로 쓴다 - 이미 올라간 카탈로그와 job_logs를 옮기지 않기 위함.
    #   설정 키는 execution_db_dsn으로 새로 두되 같은 DB를 가리킨다.
    #     exec_command: `head -n {lines} {path}` 같은 **자리표시자가 든 커맨드 한 줄**
    #     args: [{name,type,required,default,description,choices}] - 자리표시자의 타입 정의
    #     allow_extra_args: 에이전트가 정의된 인자 외에 자유 인자를 덧붙일 수 있는가
    #     host_mode: login_server(로그인 서버 고정) | target_server(LLM이 서버를 지정)
    ("command_db", 7, """
        CREATE TABLE IF NOT EXISTS execution_commands (
            id SERIAL PRIMARY KEY,
            tool_name        TEXT UNIQUE NOT NULL,
            title            TEXT NOT NULL,
            description      TEXT NOT NULL DEFAULT '',
            exec_command     TEXT NOT NULL,
            args             JSONB NOT NULL DEFAULT '[]',
            allow_extra_args BOOLEAN NOT NULL DEFAULT true,
            host_mode        TEXT NOT NULL DEFAULT 'login_server',
            enabled          BOOLEAN NOT NULL DEFAULT true,
            required_roles   TEXT[] NOT NULL DEFAULT '{}',
            updated_by       TEXT,
            updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT execution_commands_host_mode_check
                CHECK (host_mode IN ('login_server', 'target_server'))
        );
        CREATE UNIQUE INDEX IF NOT EXISTS execution_commands_title_idx
            ON execution_commands (title);
        -- 구 코드 내장 커맨드의 활성/역할/설명/실행위치. 구 system_whitelist_state.
        -- **#128에서 내장 커맨드를 없애 더 이상 읽지 않는다.** 되돌릴 수 있게 테이블만 남긴다.
        CREATE TABLE IF NOT EXISTS execution_builtin_state (
            tool_name            TEXT PRIMARY KEY,
            enabled              BOOLEAN NOT NULL DEFAULT true,
            required_roles       TEXT[],
            description_override TEXT,
            -- NULL = 코드 기본값(builtin.py)을 쓴다. 관리자가 고른 값만 들어간다(#115).
            host_mode            TEXT,
            updated_by           TEXT,
            updated_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT execution_builtin_host_mode_check
                CHECK (host_mode IN ('login_server', 'target_server'))
        );
    """),
    # v9: 내장 커맨드 상태 행이 **자동 생성**될 때 host_mode가 컬럼 기본값('target_server')으로
    #     들어가면서, 코드가 login_server로 지정한 툴(list_dir/find_files/read_file_head)의
    #     실행 위치를 조용히 덮어썼다. 그러면 host가 LLM에 노출돼 엉뚱한 서버에서 실행된다
    #     ("내 홈 파일 리스트"가 로그인 서버가 아닌 곳에서 돌던 원인 - #115).
    #     이제 **NULL = 코드 기본값을 쓰라는 뜻**이다. 관리자가 콘솔에서 고른 값만 들어간다.
    ("command_db", 9, """
        ALTER TABLE execution_builtin_state ALTER COLUMN host_mode DROP NOT NULL;
        ALTER TABLE execution_builtin_state ALTER COLUMN host_mode DROP DEFAULT;
        -- 자동 생성된 행(updated_by가 비어 있음)의 host_mode는 의미 없는 값이다.
        UPDATE execution_builtin_state SET host_mode = NULL WHERE updated_by IS NULL;
        -- 이관해 온 행도 같은 경로로 오염됐을 수 있다. 코드가 로그인 서버로 고정한 툴이
        -- target_server로 되어 있으면 그건 관리자의 선택이 아니라 사고다.
        UPDATE execution_builtin_state SET host_mode = NULL
        WHERE host_mode = 'target_server'
          AND tool_name IN ('list_dir', 'find_files', 'read_file_head');
    """),
    # v8: 커맨드 RAG 검색은 #105에서 없앴다(툴로 노출한다). 임베딩 컬럼은 그때부터 아무도 읽지
    #     않는데 업로드할 때마다 수천 건을 임베딩하느라 몇 분씩 걸리고 있었다. 통합하며 정리한다.
    ("command_db", 8, """
        DROP INDEX IF EXISTS command_catalog_embedding_idx;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embedding;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embed_model;
        ALTER TABLE command_catalog DROP COLUMN IF EXISTS embed_dim;
    """),
    # reference_path = 이 문서가 실제로 있는 위치(사내 포탈 경로 등).
    #   에이전트가 "OO 문서를 참고하세요"라고만 하면 사용자는 그 문서를 찾을 수 없다.
    #   관리자가 여기에 전체 경로를 넣어 두면 답변에 경로가 그대로 붙는다.
    #   예: 슈퍼컴 Portal (https://…) > USEFUL INFO. > 활용 가이드 > GPU 서버 활용 가이드
    ("manual_db", 7, """
        ALTER TABLE manual_files ADD COLUMN IF NOT EXISTS reference_path TEXT;
    """),
    # doc_title = 이 청크가 나온 '원본 문서' 이름(PPT 파일명 등).
    #   매뉴얼 한 건에 여러 가이드 문서가 섞여 올라오는 경우가 있다. 예를 들어 '활용 가이드'
    #   메뉴 하나를 등록하면 그 안에 GPU 서버 활용 가이드, 계정 신청 가이드 …가 다 들어 있다.
    #   manual_files.title은 메뉴 이름("활용 가이드")이고, 개별 문서 이름은 행마다 다르므로
    #   청크 단위로 들고 있어야 한다. 답변에서 문서 위치를 안내할 때
    #   reference_path(메뉴까지) + doc_title(문서 이름)로 전체 경로가 완성된다.
    #   검색에도 쓰이도록 tsv에 포함한다("GPU 서버 활용 가이드" 같은 질의가 제목으로 잡힌다).
    ("manual_db", 8, """
        ALTER TABLE manual_chunks ADD COLUMN IF NOT EXISTS doc_title TEXT;
        ALTER TABLE manual_chunks DROP COLUMN IF EXISTS tsv;
        ALTER TABLE manual_chunks ADD COLUMN tsv tsvector
            GENERATED ALWAYS AS (
                to_tsvector('simple',
                    coalesce(doc_title, '') || ' ' ||
                    coalesce(section_title, '') || ' ' || coalesce(chunk_text, ''))
            ) STORED;
        CREATE INDEX IF NOT EXISTS manual_chunks_tsv_idx ON manual_chunks USING gin (tsv);
        CREATE INDEX IF NOT EXISTS manual_chunks_doc_title_idx ON manual_chunks (doc_title);
    """),
]


# --- 설정 시드 -------------------------------------------------------------------
# credential류는 환경변수에서 만들어 넣는다(SQL에 하드코딩하지 않음).
# force=True인 항목은 매 기동 시 환경변수 값으로 덮어써서 비밀번호 변경이 자동 반영되게 한다.
def config_seed() -> list[tuple[str, str, str, bool, bool, bool]]:
    """(key, value, description, hot_reload, is_secret, force)"""
    return [
        # 주소류 기본값은 .env(환경변수)에서 읽는다 -> 배포 시 주소를 .env 한 곳에서 관리.
        # force=False라 최초 1회만 주입되고, 이후 관리자 콘솔에서 바꾼 값을 덮어쓰지 않는다.
        ("vllm_llm_base_url", os.environ.get("VLLM_LLM_BASE_URL", "http://CHANGE-ME:8000/v1"), "vLLM LLM 서버 주소 (OpenAI 호환)", True, False, False),
        ("vllm_llm_model", os.environ.get("VLLM_LLM_MODEL", "qwen3-32b"), "vLLM에 서빙 중인 LLM 모델명", True, False, False),
        ("vllm_embed_base_url", os.environ.get("VLLM_EMBED_BASE_URL", "http://CHANGE-ME:8010/v1"), "vLLM 임베딩 서버 주소", True, False, False),
        ("vllm_embed_model", os.environ.get("VLLM_EMBED_MODEL", "bge-m3"), "임베딩 모델명", True, False, False),
        ("embed_dim", os.environ.get("EMBED_DIM", "1024"), "임베딩 차원(스키마 vector(N)과 일치해야 함)", False, False, False),
        ("rerank_provider", os.environ.get("RERANK_PROVIDER", "tei"), "리랭커 종류: tei | vllm | none", True, False, False),
        ("rerank_base_url", os.environ.get("RERANK_BASE_URL", ""), "리랭커 서버 주소. 비우면 리랭킹 생략", True, False, False),
        ("rerank_model", os.environ.get("RERANK_MODEL", "bge-reranker-v2-m3"), "리랭커 모델명", True, False, False),
        ("rerank_timeout_seconds", "5", "리랭커 타임아웃(초). 초과 시 RRF 결과로 fallback", True, False, False),
        # 검색 결과 검증: 리랭커 점수가 이 값 미만이면 질문과 무관한 문서로 보고 버린다.
        # 0으로 두면 필터 없음. 올릴수록 "확인되지 않습니다"가 늘고, 내릴수록 엉뚱한 근거가 섞인다.
        # 상위 결과가 사실상 같은 문장으로 채워지는 걸 막는다(1이면 중복 제거 안 함).
        ("dedup_similarity", "0.85",
         "검색 결과 중복 제거 기준(3-gram 자카드 유사도, 1이면 비활성)", True, False, False),
        ("rerank_min_score", "0.05",
         "검색 결과로 채택할 최소 리랭커 관련도 점수(0~1, 0이면 필터 없음)", True, False, False),
        # 3-gram 축은 word_similarity(질의, 본문) 기준이라 문서 길이에 휘둘리지 않는다.
        # 올리면 정확도↑ 재현율↓. 0.2~0.4 사이가 무난하다.
        ("trgm_min_similarity", "0.3",
         "3-gram 검색축에서 후보로 받을 최소 word_similarity(0~1)", True, False, False),
        # 절차 문서에서 중간 단계가 빠지지 않도록 검색된 청크의 앞뒤를 함께 읽어 준다.
        # 0이면 확장하지 않음. 크게 잡으면 컨텍스트가 길어져 답변이 산만해진다(최대 3).
        ("manual_neighbor_window", "1",
         "매뉴얼 검색 결과에 함께 붙일 앞뒤 청크 수(0이면 붙이지 않음)", True, False, False),
        # 임베딩 모델 한도(bge-m3 8192토큰)를 넘기면 서버가 400을 돌려준다. 넘는 입력은 잘라서
        # 보낸다. 0이면 자르지 않음(모델 한도가 더 큰 경우에만).
        ("embed_max_chars", "4000",
         "임베딩에 보낼 최대 글자 수(초과분은 잘림, 0이면 자르지 않음)", True, False, False),
        # VOC 한 건이 수십만 자인 경우가 있어, 검색 결과로 넘길 때 앞부분만 자른다
        # (원문은 DB에 그대로 남는다). 0이면 자르지 않음.
        # LLM 컨텍스트(32768토큰)를 넘기지 않도록 도구 결과와 대화 이력에 상한을 둔다.
        # 실제로 매뉴얼+VOC 결과가 길어 ContextWindowExceededError(33k~35k 토큰)가 났다.
        ("voc_result_max_chars", "1500",
         "VOC 검색 결과에서 질문/답변 하나당 넘길 최대 글자 수(0이면 자르지 않음)", True, False, False),
        # 커맨드 출력도 그대로 다음 요청 프롬프트에 실린다. 상한이 없어서 nvidia-smi/job 목록
        # 몇 번에 59,360토큰이 되어 컨텍스트를 넘긴 사고가 있었다(#123).
        ("execution_result_max_chars", "4000",
         "커맨드 실행 출력을 에이전트에 넘길 최대 글자 수(0이면 자르지 않음)", True, False, False),
        # **모델이 목록을 조용히 줄이는 것**을 지시문으로 세 번 막아 봤지만 지시문은 확률이다
        # (132줄 중 22줄만 보여준 사고, #146·#150). 사용자가 반드시 봐야 하는 것은 LLM을
        # 거치지 않고 붙인다 - 진행 줄이 통했던 것과 같은 방식이다.
        # 답변에 조회 결과에 없는 IP·경로가 들어갔는지 검사해 경고를 붙인다(#154).
        # "지어내지 마라"를 지시문으로 네 번 강화했는데 계속 재발했다 - 없는 서버 IP를
        # 만들어 안내한 건은 사용자가 그 주소로 접속을 시도하므로 곧바로 사고다.
        ("answer_grounding_check", "true",
         "조회 결과에 없는 IP·경로가 답변에 있으면 그 답변을 버리고 운영팀 문의로 바꾼다"
         "(지어내기 방지). 켜져 있으면 본문은 검사 후 한 번에 나온다",
         True, False, False),
        ("rag_prefetch", "true",
         "매 질문마다 매뉴얼과 과거 사례(VOC)를 먼저 검색해 근거(문서 위치 포함)를 "
         "프롬프트에 넣는다. 모델이 검색을 건너뛰고 지어내는 것을 막는다",
         True, False, False),
        ("prefetch_route", "true",
         "질문이 '실행해야만 알 수 있는 값'을 묻는 것이면 매뉴얼·VOC 선검색을 건너뛰고 "
         "바로 실행하게 한다(LLM에 한 번 묻는다, 애매하면 검색). 끄면 항상 선검색",
         True, False, False),
        ("manual_prefetch_top_k", "5",
         "매뉴얼 선검색으로 프롬프트에 넣을 근거 문단 수(늘리면 프롬프트가 커진다). "
         "선검색이 모델의 직접 검색을 대신하므로 `search_manual` 기본값과 같게 둔다",
         True, False, False),
        ("voc_prefetch_top_k", "3",
         "VOC 선검색으로 프롬프트에 넣을 과거 사례 수(늘리면 프롬프트가 커진다)",
         True, False, False),
        ("voc_classify_batch", "12",
         "VOC 처리 주체를 LLM으로 판정할 때 한 번에 묶어 보낼 건수. "
         "늘리면 빠르지만 컨텍스트를 넘겨 배치가 통째로 실패할 수 있다",
         True, False, False),
        ("voc_classify_concurrency", "4",
         "VOC 분류를 몇 배치씩 동시에 보낼지. 늘리면 백필이 그만큼 빨라지지만 "
         "같은 vLLM을 쓰는 사용자 채팅이 그동안 느려진다",
         True, False, False),
        ("execution_raw_output", "true",
         "실행 결과 원문을 모델 답변 뒤에 그대로 붙인다(모델이 행을 줄여도 전체가 보인다)",
         True, False, False),
        ("execution_raw_output_min_lines", "2",
         "원문을 붙일 최소 줄 수. 한두 줄짜리는 답변에 이미 들어 있어 중복이다",
         True, False, False),
        ("execution_raw_output_max_chars", "20000",
         "원문 블록 자체의 상한(0이면 무제한). 에이전트에 넘기는 상한과 별개로, "
         "화면에는 더 많이 보여줄 수 있다", True, False, False),
        # 원문 뒤에 한 번 더 요약을 붙인다. LLM을 **한 번 더** 부르므로 답변이 몇 초 늦어진다.
        ("execution_raw_output_summary", "false",
         "원문 블록 뒤에 짧은 요약을 덧붙인다(LLM 추가 호출 - 응답이 몇 초 늦어진다)",
         True, False, False),
        ("manual_result_max_chars", "1500",
         "매뉴얼 검색 결과 하나당 넘길 최대 글자 수(이웃 청크 포함, 0이면 자르지 않음)",
         True, False, False),
        ("history_max_chars", "8000",
         "에이전트에 넘길 대화 이력의 최대 글자 수(넘으면 오래된 턴부터 버림)", True, False, False),
        ("embed_cache_ttl_seconds", "86400", "쿼리 임베딩 캐시 TTL(초)", True, False, False),
        ("clean_policy_version", "1", "정제 정책 버전(캐시 키에 포함)", True, False, False),
        ("search_max_top_k", "20", "검색 top_k 상한", True, False, False),
        ("search_max_candidates", "100", "리랭킹 후보 상한", True, False, False),
        ("upload_max_mb", "50", "업로드 최대 크기(MB)", True, False, False),
        ("upload_session_ttl_minutes", "60", "업로드 미리보기 세션 유효시간(분)", True, False, False),
        ("upload_source_dir", "/data/uploads",
         "매뉴얼/VOC/커맨드 카탈로그 '서버 파일에서 선택' 목록 경로(admin-console 컨테이너 내부 "
         "경로, docker-compose에서 마운트된 폴더 하위만 가능)", True, False, False),
        # 반드시 **IP**로 둔다. 이름(login-01 등)은 배포 호스트 /etc/hosts에 의존하는데,
        # 실제로 login-01이 게이트 서버가 아닌 10.0.1.7로 풀려 모든 실행이 인증 실패했다.
        # 등록 커맨드를 MCP 툴로 노출하는 개수 상한. 툴 설명이 전부 프롬프트에 실리므로
        # 무한정 늘릴 수 없다. 넘치면 남는 커맨드는 run_command로만 실행 가능하다.
        ("execution_tools_max", "80",
         "등록 커맨드를 MCP 툴로 노출할 최대 개수(툴 하나당 약 100토큰이 매 요청에 실린다)",
         False, False, False),
        ("execution_host",
         os.environ.get("EXECUTION_HOST", os.environ.get("SCHEDULER_LOGIN_HOST", "10.0.0.100")),
         "커맨드를 실행할 서버 주소(로그인 서버). 등록 커맨드 중 '로그인 서버 고정'인 것과 "
         "run_command가 여기서 실행된다. 이름 말고 **IP**로 적는다(이름 해석 사고 방지)",
         True, False, False),
        # 차단 목록. 등록 커맨드의 '추가 인자'와 미등록 커맨드(run_command)의 **모든 토큰**을
        # 이 목록으로 검사한다 - `mpirun -n 4 rm -rf /`처럼 인자를 실행하는 커맨드가 있기 때문이다.
        # 콤마 구분, 비우면 제한 없음.
        ("execution_deny_commands", DEFAULT_DENY_CSV,
         "실행을 거부할 명령 이름(콤마 구분). 커맨드의 모든 토큰을 검사한다. 비우면 제한 없음",
         True, False, False),
        # 실행 신원(runuser)은 이미 본인으로 고정돼 있지만, `phd list -u 남의계정`처럼
        # **프로그램 자신이 대상을 고르는** 옵션은 OS가 막아 주지 않는다(#140).
        ("execution_user_scope_flags", DEFAULT_USER_SCOPE_CSV,
         "다른 사용자를 지목하는 옵션(콤마 구분). 이 옵션에 본인이 아닌 계정을 주면 거부한다. "
         "`sort -u`처럼 계정과 무관한 옵션이 걸리면 목록에서 빼면 된다. 비우면 검사 안 함",
         True, False, False),

        # 도구 호출/결과를 답변에 접히는 블록으로 표시(사용자가 "생각 과정 보이게" 요청).
        # 낮을수록 학습 지식으로 지어내는 경향이 줄고 조회 결과에 충실해진다.
        ("llm_temperature", "0.2", "LLM 샘플링 temperature(0~1). 낮을수록 근거에 충실", True, False, False),
        # google-adk 1.22.1의 스트리밍 분기는 툴 호출 인자 조각을 `chunk.index or fallback_index`로
        # 모으는데, 파이썬에서 0이 거짓이라 index 0을 '없음'으로 취급한다. vLLM(hermes)이 같은
        # 호출의 조각에 index를 바꿔 보내면 인자가 잘려 `json.loads`에서 요청이 통째로 죽는다.
        # 평소에는 자동으로 논스트리밍 재시도가 붙지만, 계속 발생하면 여기서 아예 끌 수 있다
        # (답변이 한 덩어리로 오지만 툴 호출은 안정적이다).
        ("llm_streaming", "true",
         "LLM 응답을 토큰 단위로 받을지(true/false). false면 답변이 한 번에 오지만 "
         "툴 호출 인자 파싱 오류를 원천적으로 피한다", True, False, False),
        # 운영자 확인이 필요한 건을 안내할 때 붙일 접수 경로(사내 서비스 포탈의 VOC 창구).
        ("voc_intake_guide",
         "서비스 포탈 > VOC 등록 메뉴에서 AI Infra 운영팀으로 접수",
         "운영팀 문의가 필요할 때 안내할 VOC 접수 경로(실제 포탈 경로로 수정하세요)", True, False, False),
        ("show_tool_activity", "true",
         "에이전트가 호출한 도구와 그 결과를 답변에 접히는 블록으로 표시(true/false)", True, False, False),

        # 장기 메모리(사용자별)
        ("memory_enabled", "true", "장기 메모리 사용 여부(true/false)", True, False, False),
        ("memory_recent_turns", "8", "프롬프트에 주입할 최근 대화 턴 수", True, False, False),
        ("memory_top_k", "5", "장기기억에서 의미검색으로 주입할 최대 항목 수", True, False, False),
        ("memory_summarize_every", "12", "이 턴 수마다 오래된 대화를 요약해 장기기억으로 승격", True, False, False),
        ("memory_ttl_days", "180", "장기기억 보존일(0이면 무기한)", True, False, False),

        # credential류: 환경변수 기반, 매 기동 시 갱신(force=True)
        ("manual_db_dsn", dsn("manual_db"), "Manual MCP 전용 DB", False, True, True),
        ("voc_db_dsn", dsn("voc_db"), "VOC MCP 전용 DB", False, True, True),
        # Execution MCP 전용 DB. 물리적으로는 기존 command_db를 그대로 쓴다(#111에서 통합할 때
        # 이미 올라간 카탈로그와 job_logs를 옮기지 않기 위해 이름만 바꿨다).
        ("execution_db_dsn", dsn("command_db"), "Execution MCP 전용 DB", False, True, True),
        # 구 System MCP DB. 이관이 끝나면 읽지 않지만, 되돌릴 수 있게 남겨 둔다.
        ("system_db_dsn", dsn("system_db"), "구 System MCP DB(통합 이관용, 읽기 전용)",
         False, True, True),
        ("agent_session_db_dsn",
         dsn("agent_sessions_db").replace("postgresql://", "postgresql+asyncpg://"),
         "ADK DatabaseSessionService용 DB (asyncpg 스킴)", False, True, True),
        ("memory_db_dsn", dsn("memory_db"), "사용자별 장기 메모리 DB", False, True, True),
        ("redis_url", redis_url(), "임베딩 캐시용 Redis(비우면 캐시 미사용)", False, True, True),

        ("manual_mcp_url", os.environ.get("MANUAL_MCP_URL", "http://manual-mcp:8001/mcp"),
         "Agent Server가 연결할 Manual MCP 주소", False, False, False),
        ("execution_mcp_url", os.environ.get("EXECUTION_MCP_URL", "http://execution-mcp:8002/mcp"),
         "Agent Server가 연결할 Execution MCP 주소(커맨드 실행 전담)", False, False, False),
        ("voc_mcp_url", os.environ.get("VOC_MCP_URL", "http://voc-mcp:8003/mcp"),
         "Agent Server가 연결할 VOC MCP 주소", False, False, False),
        ("chart_mcp_url", os.environ.get("CHART_MCP_URL", "http://chart-mcp:8005/mcp"),
         "Agent Server가 연결할 Chart MCP 주소(비우면 차트 기능 없이 동작)", False, False, False),
        # **비워 두는 것이 기본이고 권장값이다.** 비어 있으면 차트를 답변 안에 그대로 박아
        # 보내므로(data URI) 설정도 열어 둘 포트도 필요 없다 - 폐쇄망에서 그대로 동작한다.
        # 이미지를 URL로 두고 싶을 때만(브라우저 캐시를 쓰거나 답변을 가볍게 하려면)
        # 배포 호스트 주소를 넣는다(예: http://10.0.0.30:8509 - 사내 주소다).
        ("chart_public_base_url", os.environ.get("CHART_PUBLIC_BASE_URL", ""),
         "(고급/보통 비움) 차트를 URL로 제공할 때의 사내 주소. 비우면 답변에 이미지를 직접 넣는다",
         True, False, False),
        ("chart_max_points", "200", "차트 하나에 넣을 수 있는 최대 항목 수", True, False, False),
        ("chart_retention_hours", "72",
         "생성된 차트 파일 보관 시간(시간). 지나면 자동 삭제, 0이면 삭제하지 않음", True, False, False),

        ("service_hub_mcp_url", os.environ.get("SERVICE_HUB_MCP_URL", ""),
         "유사 VOC 조회용 Service Hub MCP 주소(비우면 similar_voc 생략). 방화벽 개통 후 설정", True, False, False),
        ("voc_similar_top_k", "3", "VOC 답변에 붙일 유사 VOC 최대 개수(0이면 비활성)", True, False, False),

        # Open WebUI 기본 모델 동기화("설정" 탭의 "Open WebUI 기본 모델 동기화" 버튼용).
        # API 키는 Open WebUI 관리자 계정으로 로그인 -> 설정 -> 계정 -> API 키에서 발급.
        # **도커 네트워크 안에서** 관리자 콘솔이 Open WebUI API를 부를 때 쓰는 주소다.
        # 8080은 Open WebUI 컨테이너의 내부 포트이고, 사용자가 브라우저로 쓰는 8502는
        # 그 8080에 매핑된 호스트 포트다. 여기에 8502를 넣으면 안 된다(내부망에서 그 포트는 없다).
        # 컨테이너 이름·포트를 바꿨다면 그때 이 값을 고친다.
        ("openwebui_base_url", os.environ.get("OPENWEBUI_BASE_URL", "http://open-webui:8080"),
         "관리자 콘솔이 Open WebUI API를 부를 도커 내부 주소. 사용자 접속 주소(8502)가 아니다",
         True, False, False),
        ("openwebui_admin_api_key", "", "Open WebUI 관리자 API 키(기본 모델 동기화용, 비우면 동기화 생략)", True, True, False),

        # --- 내부 신뢰 경계 인증 (#139) -------------------------------------------------
        # MCP는 X-User-Id를 그대로 믿고 그 계정 권한으로 커맨드를 실행한다. MCP 포트가
        # 호스트에 열려 있으면 같은 망의 누구나 그 헤더를 붙여 남의 계정으로 실행할 수 있다.
        # agent-server와 MCP가 **같은 DB에서 읽는** 무작위 비밀값으로 그 경로를 막는다.
        # 관리자가 손댈 일이 없다(한 번 심기고 그대로 유지된다 - force=False).
        ("mcp_shared_secret", secrets.token_urlsafe(32),
         "agent-server ↔ MCP 내부 호출 인증용 비밀값(자동 생성, 건드리지 마세요)",
         True, True, False),
        # agent-server의 /v1/*을 호출할 때 요구할 API 키. Open WebUI의 연결(Connections)에
        # 넣은 API 키와 같은 값을 여기 넣으면 그때부터 인증이 강제된다.
        # **비우면 인증 없이 누구나 호출할 수 있다**(그 상태면 기동 로그에 경고가 찍힌다).
        ("agent_api_key", "",
         "agent-server /v1/* 호출에 요구할 API 키(Open WebUI 연결에 넣은 값과 동일하게). "
         "비우면 인증하지 않는다", True, True, False),
        # 위 openwebui_base_url은 **콘솔 -> Open WebUI API**용 내부 주소(8080)다. 사용자가
        # 브라우저로 들어가는 주소(8502)는 별개라, 안내 문구에 쓸 값으로 따로 둔다.
        ("openwebui_public_url",
         os.environ.get("OPENWEBUI_PUBLIC_URL", "http://10.0.0.30:8502"),
         "사용자가 브라우저로 접속하는 Open WebUI 주소(안내용). 콘솔이 API를 부르는 "
         "openwebui_base_url(도커 내부 8080)과는 다른 값이다", True, False, False),

        ("agent_system_instruction", AGENT_INSTRUCTION, "ADK 루트 에이전트 system instruction", False, False, False),
    ]




async def ensure_databases():
    """존재하지 않는 DB를 만든다(볼륨이 이미 있어 init-db가 실행되지 않은 경우 대비)."""
    conn = await asyncpg.connect(dsn("postgres"))
    try:
        for db in APP_DBS:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", db)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{db}"')
                print(f"[migrate] created database {db}")
    finally:
        await conn.close()


async def apply_migrations():
    by_db: dict[str, list[tuple[int, str]]] = {}
    for db, version, sql in MIGRATIONS:
        by_db.setdefault(db, []).append((version, sql))

    for db, items in by_db.items():
        conn = await asyncpg.connect(dsn(db))
        try:
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
            """)
            applied = {r["version"] for r in await conn.fetch("SELECT version FROM schema_migrations")}
            for version, sql in sorted(items):
                if version in applied:
                    continue
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute("INSERT INTO schema_migrations (version) VALUES ($1)", version)
                print(f"[migrate] {db}: applied v{version}")
        finally:
            await conn.close()


async def seed_config():
    conn = await asyncpg.connect(dsn("platform_config"))
    try:
        for key, value, desc, hot, secret, force in config_seed():
            if force:
                # 환경변수 기반 값: 항상 최신으로 갱신 (비밀번호 변경 자동 반영)
                await conn.execute("""
                    INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by, updated_at)
                    VALUES ($1,$2,$3,$4,$5,'bootstrap', now())
                    ON CONFLICT (key) DO UPDATE
                    SET value = EXCLUDED.value, description = EXCLUDED.description,
                        hot_reload = EXCLUDED.hot_reload, is_secret = EXCLUDED.is_secret,
                        updated_by = 'bootstrap', updated_at = now()
                """, key, value, desc, hot, secret)
            else:
                # 운영자가 콘솔에서 바꿀 수 있는 값: 없을 때만 삽입(덮어쓰지 않음)
                await conn.execute("""
                    INSERT INTO platform_settings (key, value, description, hot_reload, is_secret, updated_by)
                    VALUES ($1,$2,$3,$4,$5,'bootstrap')
                    ON CONFLICT (key) DO UPDATE
                    SET description = EXCLUDED.description,
                        hot_reload = EXCLUDED.hot_reload,
                        is_secret = EXCLUDED.is_secret
                """, key, value, desc, hot, secret)
        print("[migrate] config seeded")
    finally:
        await conn.close()


async def import_execution_registry():
    """구 Command/System MCP의 등록 내용을 Execution MCP의 한 테이블로 옮긴다(#111).

    SQL 마이그레이션으로 못 하는 이유가 둘이다.
      1) `system_custom_commands`는 **다른 데이터베이스**(system_db)에 있다.
      2) 한글 이름에서 ASCII 툴 이름을 만드는 규칙이 파이썬 코드(registry.tool_name_for)에 있다.
    여러 번 돌려도 안전하다 - 이미 있는 title은 건너뛴다(관리자가 콘솔에서 고친 값을 덮지 않는다).
    """
    # tool_name_for는 shared에 있다. db-init 컨테이너에는 mcp_servers가 마운트되지 않으므로
    # MCP 쪽 모듈을 import하려 하면 이관이 조용히 건너뛰어진다(실제로 그렇게 실패했다).
    conn = await asyncpg.connect(dsn("command_db"))
    try:
        taken = {r["tool_name"] for r in
                 await conn.fetch("SELECT tool_name FROM execution_commands")}
        have_titles = {r["title"] for r in
                       await conn.fetch("SELECT title FROM execution_commands")}
        moved = unrunnable = 0
        # **항상** 출처와 결과 건수를 찍는다. 옮길 게 없을 때 아무 말도 하지 않으면
        # "이관이 된 건지 안 된 건지" 알 수 없다 - #112에서 실제로 그래서 헷갈렸다.
        src_catalog = await conn.fetchval("SELECT count(*) FROM command_catalog") or 0
        already = len(have_titles)

        # (1) 커맨드 카탈로그(매뉴얼 엑셀 업로드본). 인자 정의가 없고 자유 인자를 허용하던 것들이라
        #     allow_extra_args=true, 로그인 서버 고정으로 옮긴다(지금 동작과 같다).
        for r in await conn.fetch(
                "SELECT name, description, exec_command FROM command_catalog ORDER BY name"):
            title = (r["name"] or "").strip()
            if not title or title in have_titles:
                continue
            exec_command = (r["exec_command"] or "").strip() or title
            # 실행 커맨드 열이 비어 있으면 예전에는 '이름'을 그대로 실행했다. 이름이 한글이면
            # 실행될 수 없는 커맨드인데, 툴로 노출되면 프롬프트 예산만 잡아먹고 매번 실패한다.
            # 옮기기는 하되 **비활성**으로 넣어, 관리자가 콘솔에서 커맨드를 채워 켜게 한다.
            runnable = exec_command.isascii()
            if not runnable:
                unrunnable += 1
            name = tool_name_for(title, taken, exec_command)
            taken.add(name)
            have_titles.add(title)
            await conn.execute(
                """
                INSERT INTO execution_commands
                    (tool_name, title, description, exec_command, args, allow_extra_args,
                     host_mode, enabled, updated_by)
                VALUES ($1,$2,$3,$4,'[]'::jsonb, true, 'login_server', $5, 'migrate')
                ON CONFLICT (tool_name) DO NOTHING
                """,
                name, title, (r["description"] or "").strip(), exec_command, runnable)
            moved += 1

        # (2) 콘솔에서 등록한 System MCP 커스텀 커맨드(다른 DB). argv 리스트 + params를
        #     새 형식(커맨드 한 줄 + args 정의)으로 바꿔 옮긴다.
        rows = []
        try:
            sysconn = await asyncpg.connect(dsn("system_db"))
        except Exception as e:  # noqa: BLE001
            print(f"[migrate] system_db에 접속하지 못해 커스텀 커맨드 이관을 건너뜁니다: {e}")
            sysconn = None
        if sysconn is not None:
            try:
                rows = await sysconn.fetch(
                    "SELECT tool_name, description, argv_template, params, required_roles, "
                    "enabled, host_mode FROM system_custom_commands")
            except Exception as e:  # noqa: BLE001
                print(f"[migrate] 구 System MCP 테이블을 읽지 못했습니다(무시): {e}")
                rows = []
            finally:
                await sysconn.close()

            for r in rows:
                title = (r["tool_name"] or "").strip()
                if not title or title in have_titles:
                    continue
                argv = json.loads(r["argv_template"]) if isinstance(r["argv_template"], str) \
                    else (r["argv_template"] or [])
                params = json.loads(r["params"]) if isinstance(r["params"], str) \
                    else (r["params"] or [])
                # argv 리스트 -> 한 줄. 토큰에 공백이 있으면 따옴표로 묶어 원래 경계를 지킨다.
                exec_command = " ".join(
                    (f'"{t}"' if " " in str(t) else str(t)) for t in argv)
                args = [{"name": p.get("name"), "type": p.get("type", "str"),
                         "required": True, "default": "", "description": ""} for p in params]
                taken.add(title)
                have_titles.add(title)
                await conn.execute(
                    """
                    INSERT INTO execution_commands
                        (tool_name, title, description, exec_command, args, allow_extra_args,
                         host_mode, enabled, required_roles, updated_by)
                    VALUES ($1,$2,$3,$4,$5::jsonb, false, $6, $7, $8, 'migrate')
                    ON CONFLICT (tool_name) DO NOTHING
                    """,
                    title, title, (r["description"] or "").strip(), exec_command,
                    json.dumps(args, ensure_ascii=False), r["host_mode"] or "target_server",
                    r["enabled"], list(r["required_roles"] or []))
                moved += 1

            # 구 내장 커맨드(화이트리스트)의 on/off·설명은 더 이상 옮기지 않는다 —
            # 내장 커맨드 자체를 없앴다(#128). execution_builtin_state 테이블은 되돌릴 수 있게
            # 남겨 두지만 읽는 코드가 없다.

        src_custom = 0 if sysconn is None else len(rows)
        total_now = await conn.fetchval("SELECT count(*) FROM execution_commands") or 0
        print(f"[migrate] execution 이관: 카탈로그 {src_catalog}건 · 구 커스텀 커맨드 "
              f"{src_custom}건 · 이미 옮겨져 있던 것 {already}건 → 신규 {moved}건, "
              f"현재 등록 커맨드 총 {total_now}건")
        if src_catalog == 0 and src_custom == 0 and total_now == 0:
            print("[migrate] 옮길 커맨드가 없습니다. 구 카탈로그가 비어 있다는 뜻이니, "
                  "관리자 콘솔 실행 탭에서 직접 등록하거나 엑셀로 일괄 등록하세요.")
        if unrunnable:
            print(f"[migrate] 그중 {unrunnable}건은 실행 커맨드가 비어 있어(이름이 한글) "
                  "**비활성**으로 넣었습니다. 관리자 콘솔 실행 탭에서 실행 커맨드를 채우고 켜세요.")
    finally:
        await conn.close()


async def main():
    await ensure_databases()
    await apply_migrations()
    await import_execution_registry()
    await seed_config()
    print("[migrate] done")


if __name__ == "__main__":
    asyncio.run(main())
