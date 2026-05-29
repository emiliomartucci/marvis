-- v1.0.0 - 2026-04-08 - Extend inbox treatment taxonomy with read_save

PRAGMA foreign_keys = OFF;

CREATE TABLE inbox_items_new (
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
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    topic TEXT NOT NULL DEFAULT 'general'
        CHECK (topic IN ('ai-news', 'ai-products', 'tooling', 'security-devtools', 'pv-energy', 'strategy-business', 'policy-politics', 'general')),
    treatment TEXT NOT NULL DEFAULT 'read'
        CHECK (treatment IN ('read', 'save', 'read_save', 'ignore'))
);

INSERT INTO inbox_items_new (
    id, source, source_item_id, dedup_key, status, title, content, url, source_path,
    metadata_json, candidate_programs, default_program, created_by, workspace_id,
    created_at, updated_at, topic, treatment
)
SELECT
    id, source, source_item_id, dedup_key, status, title, content, url, source_path,
    metadata_json, candidate_programs, default_program, created_by, workspace_id,
    created_at, updated_at, topic,
    CASE WHEN treatment = 'read_save' THEN 'read_save' ELSE treatment END
FROM inbox_items;

DROP TABLE inbox_items;
ALTER TABLE inbox_items_new RENAME TO inbox_items;

CREATE INDEX IF NOT EXISTS idx_inbox_items_source_created_at
    ON inbox_items(source, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_status
    ON inbox_items(workspace_id, status, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS idx_inbox_items_dedup
    ON inbox_items(workspace_id, source, dedup_key);

CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_topic_treatment
    ON inbox_items(workspace_id, topic, treatment, created_at DESC);

INSERT OR IGNORE INTO schema_versions (version) VALUES (56);

PRAGMA foreign_keys = ON;
