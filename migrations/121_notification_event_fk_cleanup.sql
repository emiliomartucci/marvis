-- v1.0.0 - 2026-05-10 - Cleanup legacy orphan notification event references
--
-- notifications.event_id already declares ON DELETE SET NULL, but older DBs
-- can contain orphan values from periods where SQLite foreign_keys was not
-- consistently enforced. Normalize those rows before Plan 0 canary gates rely
-- on PRAGMA foreign_key_check as a hard acceptance criterion.

BEGIN IMMEDIATE;

UPDATE notifications
SET event_id = NULL
WHERE event_id IS NOT NULL
  AND NOT EXISTS (
      SELECT 1
      FROM events
      WHERE events.id = notifications.event_id
  );

INSERT OR IGNORE INTO schema_versions (version) VALUES (121);

COMMIT;
