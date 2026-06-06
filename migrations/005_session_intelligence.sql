-- 005_session_intelligence.sql
-- Add conversation tracking, hibernation state, and metrics caching

ALTER TABLE sessions_meta ADD COLUMN conversation_id TEXT;
ALTER TABLE sessions_meta ADD COLUMN hibernated INTEGER DEFAULT 0;
ALTER TABLE sessions_meta ADD COLUMN hibernated_at TEXT;
ALTER TABLE sessions_meta ADD COLUMN model TEXT;
ALTER TABLE sessions_meta ADD COLUMN last_context_pct REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_usd REAL;
ALTER TABLE sessions_meta ADD COLUMN last_message_count INTEGER;
ALTER TABLE sessions_meta ADD COLUMN last_metrics_at TEXT;
ALTER TABLE sessions_meta ADD COLUMN auto_hibernate_minutes INTEGER DEFAULT 30;

CREATE INDEX IF NOT EXISTS idx_sessions_conversation ON sessions_meta(conversation_id);
CREATE INDEX IF NOT EXISTS idx_sessions_hibernated ON sessions_meta(hibernated);
