-- 024_chat_messages.sql
-- Chat messages for agent conversations
-- 2026-03-03

CREATE TABLE IF NOT EXISTS chat_messages (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    metadata_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_chat_messages_agent_id ON chat_messages(agent_id, created_at DESC);

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (24, datetime('now', 'utc'));
