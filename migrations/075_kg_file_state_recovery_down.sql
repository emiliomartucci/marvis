-- Rollback migration 075 — drop file_state table + indices
DROP INDEX IF EXISTS idx_file_state_path;
DROP INDEX IF EXISTS idx_file_state_indexed_at;
DROP TABLE IF EXISTS file_state;
DELETE FROM schema_versions WHERE version=75;
