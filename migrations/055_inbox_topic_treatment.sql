-- v1.0.0 - 2026-04-08 - Inbox topic/treatment taxonomy v1

ALTER TABLE inbox_items ADD COLUMN topic TEXT NOT NULL DEFAULT 'general'
    CHECK (topic IN ('ai-news', 'ai-products', 'tooling', 'security-devtools', 'pv-energy', 'strategy-business', 'policy-politics', 'general'));

ALTER TABLE inbox_items ADD COLUMN treatment TEXT NOT NULL DEFAULT 'read'
    CHECK (treatment IN ('read', 'save', 'ignore'));

CREATE INDEX IF NOT EXISTS idx_inbox_items_workspace_topic_treatment
    ON inbox_items(workspace_id, topic, treatment, created_at DESC);

INSERT OR IGNORE INTO schema_versions (version) VALUES (55);
