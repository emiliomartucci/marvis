-- Rollback per migration 103 (ingest_skipped tracking).
--
-- Drop sicuro: la tabella e' append-only audit, nessun consumer dipende per
-- correctness. La sidebar "Ignorati" e il dedup_files[] response field
-- semplicemente mostreranno vuoto post-rollback.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_ingest_skipped_sha256;
DROP INDEX IF EXISTS idx_ingest_skipped_project_created;
DROP TABLE IF EXISTS ingest_skipped;

DELETE FROM schema_versions WHERE version = 103;

COMMIT;
