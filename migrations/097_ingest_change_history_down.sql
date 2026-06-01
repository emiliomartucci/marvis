-- Migration 097 down: drop ingest_change_history audit table
-- v1.0.0 - 2026-04-29 - Phase 1.5 P1.5.E4 rollback
--
-- WARNING: irreversibly drops audit history for ingest_pending changes.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_change_hist_ingest_id;
DROP TABLE IF EXISTS ingest_change_history;

DELETE FROM schema_versions WHERE version = 97;

COMMIT;
