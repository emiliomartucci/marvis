-- 024_agent_tokens.sql
-- Per-agent Bearer tokens with scoped permissions
-- Replaces single shared TASKS_API_TOKEN with per-agent tokens stored in DB
-- 2026-03-03

CREATE TABLE IF NOT EXISTS agent_tokens (
    id TEXT PRIMARY KEY,
    agent_name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,  -- SHA-256 hex digest of the raw token
    scopes TEXT NOT NULL DEFAULT '[]',  -- JSON array of scope strings
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    last_used_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_tokens_agent_name ON agent_tokens(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_active ON agent_tokens(is_active) WHERE is_active = 1;
CREATE INDEX IF NOT EXISTS idx_agent_tokens_hash ON agent_tokens(token_hash);

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (26, datetime('now', 'utc'));
