-- Postgres 최초 기동 시 DB만 생성한다.
-- 스키마와 설정 시드는 shared/migrations.py(db-init 서비스)가 담당한다.
--   - credential을 SQL에 하드코딩하지 않기 위해(환경변수 기반 주입)
--   - init-db는 최초 1회만 실행되므로 이후 스키마 변경을 반영할 수 없기 때문
CREATE DATABASE platform_config;
CREATE DATABASE manual_db;
CREATE DATABASE voc_db;
CREATE DATABASE command_db;
CREATE DATABASE system_db;
CREATE DATABASE agent_sessions_db;
CREATE DATABASE memory_db;
CREATE DATABASE langfuse;
