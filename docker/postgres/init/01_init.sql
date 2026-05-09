-- ============================================================
-- 01_init.sql — agentdb 初始化
-- 由 docker-compose postgres 服务首次启动时自动执行
-- ============================================================

-- Extension for UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Extension for full-text search (Chinese-compatible via unaccent)
CREATE EXTENSION IF NOT EXISTS "unaccent";

-- Placeholder comment: P2 will add agent_traces, agent_cases tables via Alembic
