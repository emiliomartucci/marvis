-- Migration 033: Add owner tracking to sessions
-- Track which user owns a tmux session for MCP client attribution

ALTER TABLE sessions_meta ADD COLUMN owner_id TEXT DEFAULT NULL;

-- Index for lookups by owner
CREATE INDEX IF NOT EXISTS idx_sessions_owner ON sessions_meta(owner_id);

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (33, datetime('now', 'utc'));
