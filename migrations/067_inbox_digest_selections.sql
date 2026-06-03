-- v1.0.0 - 2026-04-14 - separate digest selection state layer

CREATE TABLE IF NOT EXISTS inbox_digest_selections (
    id TEXT PRIMARY KEY,
    inbox_item_id TEXT NOT NULL,
    digest_cycle_key TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('visible', 'overflow', 'expired')),
    domain_key TEXT NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    rank_in_domain INTEGER,
    expires_at TEXT,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at TEXT DEFAULT (datetime('now','utc')),
    updated_at TEXT DEFAULT (datetime('now','utc')),
    FOREIGN KEY (inbox_item_id) REFERENCES inbox_items(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_item_cycle
    ON inbox_digest_selections(workspace_id, inbox_item_id, digest_cycle_key);

CREATE UNIQUE INDEX IF NOT EXISTS idx_digest_selection_active_item
    ON inbox_digest_selections(workspace_id, inbox_item_id)
    WHERE state IN ('visible', 'overflow');

CREATE INDEX IF NOT EXISTS idx_digest_selection_cycle_state_domain
    ON inbox_digest_selections(workspace_id, digest_cycle_key, state, domain_key, rank_in_domain);

INSERT OR IGNORE INTO schema_versions (version) VALUES (67);
