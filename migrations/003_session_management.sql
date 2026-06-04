-- v1.0.0 - 2026-02-25 - Add pin, sort_order, group for session management

ALTER TABLE sessions_meta ADD COLUMN pinned INTEGER DEFAULT 0;
ALTER TABLE sessions_meta ADD COLUMN sort_order INTEGER DEFAULT 0;
ALTER TABLE sessions_meta ADD COLUMN group_name TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (3);
