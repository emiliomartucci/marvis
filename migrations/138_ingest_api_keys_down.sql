-- Rollback migration 138: drop ingest_api_keys.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U1

DROP INDEX IF EXISTS idx_ingest_api_keys_active;
DROP INDEX IF EXISTS idx_ingest_api_keys_hash;
DROP TABLE IF EXISTS ingest_api_keys;

DELETE FROM schema_versions WHERE version = 138;
