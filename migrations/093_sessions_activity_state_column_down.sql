-- Migration 093 down — drop sessions_meta.activity_state

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta
    DROP COLUMN activity_state;

DELETE FROM schema_versions WHERE version = 93;

COMMIT;
