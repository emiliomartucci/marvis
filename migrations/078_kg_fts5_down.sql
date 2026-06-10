-- Down migration 078 — KG Phase 6.5: drop FTS5 virtual table + triggers.
--
-- Rollback plan: safe drop, no data loss (the FTS index is derived state;
-- the source of truth is graph_nodes).

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS graph_nodes_fts_insert;
DROP TRIGGER IF EXISTS graph_nodes_fts_update;
DROP TRIGGER IF EXISTS graph_nodes_fts_delete;
DROP TABLE IF EXISTS graph_nodes_fts;

DELETE FROM schema_versions WHERE version = 78;
COMMIT;
