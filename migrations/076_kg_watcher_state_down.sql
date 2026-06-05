-- Rollback migration 076 — drop kg_watcher_state table
DROP TABLE IF EXISTS kg_watcher_state;
DELETE FROM schema_versions WHERE version=76;
