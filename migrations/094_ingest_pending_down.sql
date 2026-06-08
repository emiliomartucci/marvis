-- Down migration 094: drop ingest_pending.
--
-- Use only before any real ingest rows matter, or after exporting the table.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_ingest_pending_lock;
DROP INDEX IF EXISTS idx_ingest_pending_created;
DROP INDEX IF EXISTS idx_ingest_pending_project;
DROP INDEX IF EXISTS idx_ingest_pending_status;
DROP TABLE IF EXISTS ingest_pending;

DELETE FROM schema_versions WHERE version = 94;

COMMIT;
