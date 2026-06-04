-- v1.0.0 - 2026-04-07 - Inbox ingestion MVP core

CREATE TABLE IF NOT EXISTS inbox_items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    source_item_id TEXT,
    dedup_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'received',
    title TEXT,
    content TEXT,
    url TEXT,
    source_path TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    candidate_programs TEXT NOT NULL DEFAULT '[]',
    default_program TEXT,
    created_by TEXT NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_inbox_items_source_created_at
    ON inbox_items(source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_status
    ON inbox_items(workspace_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedup
    ON inbox_items(workspace_id, source, dedup_key);

INSERT OR IGNORE INTO schema_versions (version) VALUES (53);
