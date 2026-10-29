BEGIN IMMEDIATE;
DROP TABLE IF EXISTS access_denials;
DELETE FROM schema_versions WHERE version = 189;
COMMIT;
