-- v1.0.0 - 2026-04-10 - TL;DR column + source_scores table for inbox Action View

ALTER TABLE inbox_items ADD COLUMN tldr TEXT;

CREATE TABLE IF NOT EXISTS source_scores (
    id TEXT PRIMARY KEY,
    source_key TEXT NOT NULL,
    score INTEGER NOT NULL DEFAULT 0,
    upvotes INTEGER NOT NULL DEFAULT 0,
    downvotes INTEGER NOT NULL DEFAULT 0,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_source_scores_key ON source_scores(workspace_id, source_key);

INSERT OR IGNORE INTO schema_versions (version) VALUES (59);
