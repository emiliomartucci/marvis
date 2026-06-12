-- Migration 041: Multi-tenancy foundation — workspaces table + workspace_id on core tables
-- Pattern: ALTER ADD COLUMN (no REFERENCES/DEFAULT — crashes with PRAGMA foreign_keys=ON in SQLite 3.45)
-- Then: UPDATE backfill → CREATE INDEX
-- Idempotency: _column_exists() in db.py handles partial failure recovery

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    created_at DATETIME DEFAULT (datetime('now','utc'))
);

-- Default workspace for existing single-user installation
INSERT OR IGNORE INTO workspaces (id, slug, display_name)
VALUES ('ws_default', 'default', 'Default Workspace');

-- Add workspace_id to all core tables (10 tables)
-- Each follows: ADD COLUMN → UPDATE backfill → INDEX

ALTER TABLE teams ADD COLUMN workspace_id TEXT;
UPDATE teams SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE users ADD COLUMN workspace_id TEXT;
UPDATE users SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE agent_tokens ADD COLUMN workspace_id TEXT;
UPDATE agent_tokens SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE tasks ADD COLUMN workspace_id TEXT;
UPDATE tasks SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE pull_requests ADD COLUMN workspace_id TEXT;
UPDATE pull_requests SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE sessions_meta ADD COLUMN workspace_id TEXT;
UPDATE sessions_meta SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE notifications ADD COLUMN workspace_id TEXT;
UPDATE notifications SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE documents ADD COLUMN workspace_id TEXT;
UPDATE documents SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE events ADD COLUMN workspace_id TEXT;
UPDATE events SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

ALTER TABLE learnings ADD COLUMN workspace_id TEXT;
UPDATE learnings SET workspace_id = 'ws_default' WHERE workspace_id IS NULL;

-- Composite indexes for query patterns: WHERE workspace_id = ? AND ...
CREATE INDEX IF NOT EXISTS idx_teams_workspace ON teams(workspace_id);
CREATE INDEX IF NOT EXISTS idx_users_workspace ON users(workspace_id);
CREATE INDEX IF NOT EXISTS idx_agent_tokens_workspace ON agent_tokens(workspace_id);
CREATE INDEX IF NOT EXISTS idx_tasks_ws_status ON tasks(workspace_id, status);
CREATE INDEX IF NOT EXISTS idx_tasks_ws_project ON tasks(workspace_id, project);
CREATE INDEX IF NOT EXISTS idx_prs_ws ON pull_requests(workspace_id);
CREATE INDEX IF NOT EXISTS idx_sessions_ws ON sessions_meta(workspace_id);
CREATE INDEX IF NOT EXISTS idx_notifications_ws ON notifications(workspace_id);
CREATE INDEX IF NOT EXISTS idx_events_ws ON events(workspace_id);
CREATE INDEX IF NOT EXISTS idx_documents_ws ON documents(workspace_id);
CREATE INDEX IF NOT EXISTS idx_learnings_ws ON learnings(workspace_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (41);
