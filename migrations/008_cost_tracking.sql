-- v1.0.0 - 2026-02-26 - Cost tracking: project_slug, session_uuid, session_costs

-- Add project_slug and session_uuid to sessions_meta
ALTER TABLE sessions_meta ADD COLUMN project_slug TEXT;
ALTER TABLE sessions_meta ADD COLUMN session_uuid TEXT;

-- Migrate group_name to project_slug where they match
UPDATE sessions_meta SET project_slug = group_name
WHERE group_name IS NOT NULL AND project_slug IS NULL;

-- Create session_costs table (conversation_id as natural PK)
CREATE TABLE IF NOT EXISTS session_costs (
    conversation_id TEXT PRIMARY KEY,
    session_name TEXT,
    project_slug TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    message_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Composite index for cost aggregation queries (covers GROUP BY + date filter)
CREATE INDEX IF NOT EXISTS idx_session_costs_project_updated ON session_costs(project_slug, updated_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions_meta(project_slug);
CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions_meta(session_uuid);

-- UUID backfill handled by Python in run_migrations() (NOT SQLite randomblob)
