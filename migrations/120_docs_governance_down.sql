-- Rollback for migration 120_docs_governance.sql.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_docs_triage_commit;
DROP INDEX IF EXISTS idx_docs_triage_pr;
DROP INDEX IF EXISTS idx_docs_triage_layer_created;
DROP TABLE IF EXISTS docs_triage_decisions;
DELETE FROM schema_versions WHERE version = 120;

COMMIT;
