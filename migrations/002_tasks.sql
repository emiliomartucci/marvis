-- v1.0.0 - 2026-02-25 - Shared Task System schema

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    project TEXT NOT NULL,
    priority TEXT NOT NULL DEFAULT 'medium',
    created_by TEXT NOT NULL,
    assigned_to TEXT,
    source TEXT NOT NULL,
    source_ref TEXT,
    tags TEXT DEFAULT '[]',
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);
CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
CREATE INDEX IF NOT EXISTS idx_tasks_created_by ON tasks(created_by);
CREATE INDEX IF NOT EXISTS idx_tasks_source ON tasks(source, source_ref);

-- Deduplication: prevent same source creating duplicate tasks
CREATE UNIQUE INDEX IF NOT EXISTS idx_tasks_dedup
    ON tasks(source, source_ref) WHERE source_ref IS NOT NULL AND deleted_at IS NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (2);
