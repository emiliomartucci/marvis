BEGIN IMMEDIATE;

DROP TABLE IF EXISTS file_acl;
DROP INDEX IF EXISTS idx_file_meta_confidential;
DROP TABLE IF EXISTS file_meta;
ALTER TABLE documents DROP COLUMN confidential;
DELETE FROM schema_versions WHERE version = 162;

COMMIT;
