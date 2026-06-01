-- Migration 092 down — drop sessions_meta.activity_state_updated_at
--
-- SQLite 3.35+ supports DROP COLUMN. Marvis runs on Debian bookworm with
-- SQLite 3.40+, so DROP COLUMN is safe. Older systems would need a table
-- rebuild — not applicable here.

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta
    DROP COLUMN activity_state_updated_at;

DELETE FROM schema_versions WHERE version = 92;

COMMIT;
