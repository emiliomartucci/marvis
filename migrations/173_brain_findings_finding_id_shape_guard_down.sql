-- Down-migration 173 — remove the brain_findings finding_id shape guard.
--
-- Drops the two BEFORE-write triggers added by
-- migrations/173_brain_findings_finding_id_shape_guard.sql. No data change: the
-- triggers validate writes only, so removing them cannot alter stored rows.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_brain_findings_finding_id_shape_insert;
DROP TRIGGER IF EXISTS trg_brain_findings_finding_id_shape_update;

DELETE FROM schema_versions WHERE version = 173;

COMMIT;
