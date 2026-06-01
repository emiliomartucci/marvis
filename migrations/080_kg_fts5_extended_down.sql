-- Down migration 080 — KG Phase 6.6: drop tasks_fts / inbox_items_fts /
-- learnings_fts and their sync triggers.
--
-- Rollback plan: safe drop, no data loss. The FTS indices are derived state;
-- the source of truth is tasks / inbox_items / learnings.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS tasks_fts_insert;
DROP TRIGGER IF EXISTS tasks_fts_update;
DROP TRIGGER IF EXISTS tasks_fts_delete;
DROP TABLE IF EXISTS tasks_fts;

DROP TRIGGER IF EXISTS inbox_items_fts_insert;
DROP TRIGGER IF EXISTS inbox_items_fts_update;
DROP TRIGGER IF EXISTS inbox_items_fts_delete;
DROP TABLE IF EXISTS inbox_items_fts;

DROP TRIGGER IF EXISTS learnings_fts_insert;
DROP TRIGGER IF EXISTS learnings_fts_update;
DROP TRIGGER IF EXISTS learnings_fts_delete;
DROP TABLE IF EXISTS learnings_fts;

DELETE FROM schema_versions WHERE version = 80;
COMMIT;
