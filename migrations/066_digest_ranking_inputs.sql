-- v1.0.0 - 2026-04-14 - persist canonical domain and freshness inputs for digest ranking

ALTER TABLE inbox_items ADD COLUMN domain_key TEXT;
ALTER TABLE inbox_items ADD COLUMN published_at TEXT;
ALTER TABLE inbox_items ADD COLUMN freshness_at TEXT;

CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_domain_freshness
    ON inbox_items(workspace_id, domain_key, freshness_at DESC, created_at DESC);

INSERT OR IGNORE INTO schema_versions (version) VALUES (66);
