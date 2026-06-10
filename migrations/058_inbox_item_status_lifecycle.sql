-- v1.0.0 - 2026-04-10 - Inbox item status lifecycle: ignore_reason, decided_by, decided_at

-- status already exists (migration 053, DEFAULT 'received')
-- Add only the missing lifecycle columns

ALTER TABLE inbox_items ADD COLUMN ignore_reason TEXT
    CHECK (ignore_reason IN ('duplicate','spam','not_interested','not_relevant','custom'));

ALTER TABLE inbox_items ADD COLUMN decided_by TEXT;

ALTER TABLE inbox_items ADD COLUMN decided_at TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (58);
