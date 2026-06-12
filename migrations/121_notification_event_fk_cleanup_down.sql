-- Rollback for migration 121_notification_event_fk_cleanup.sql.
--
-- This data cleanup is intentionally irreversible: the previous event rows no
-- longer exist, and restoring orphan references would reintroduce FK failures.

BEGIN IMMEDIATE;

DELETE FROM schema_versions WHERE version = 121;

COMMIT;
