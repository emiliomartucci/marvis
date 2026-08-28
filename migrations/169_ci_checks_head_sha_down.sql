DROP INDEX IF EXISTS idx_ci_checks_task_head;

ALTER TABLE ci_checks DROP COLUMN head_sha;

DELETE FROM schema_versions WHERE version = 169;
