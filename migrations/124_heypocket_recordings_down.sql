-- Rollback migration 124: remove heypocket_recordings state machine.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_heypocket_recordings_updated_at;
DROP INDEX IF EXISTS idx_heypocket_recordings_sha256;
DROP INDEX IF EXISTS idx_heypocket_recordings_cursor;
DROP INDEX IF EXISTS idx_heypocket_recordings_state;
DROP TABLE IF EXISTS heypocket_recordings;

DELETE FROM schema_versions WHERE version = 124;

COMMIT;
