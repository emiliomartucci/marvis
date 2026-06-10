-- Down migration 095: remove intent-first KG columns and activity ledger.
--
-- CAVEAT: ALTER TABLE DROP COLUMN requires SQLite 3.35+.
-- Operator preflight before running this down migration:
--   SELECT sqlite_version();
-- Abort on SQLite < 3.35: this file will fail after dropping the auxiliary
-- tables, leaving a partial rollback unless the transaction is restored from a
-- backup. For older SQLite, do not run this file directly; either restore the
-- pre-095 backup or use the graph_edges table-rebuild rollback pattern from
-- migrations 073/085, preserving every graph_edges column except
-- weight/last_touched_at and recreating the remaining indexes/triggers.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_kg_edge_activity_edge;
DROP INDEX IF EXISTS idx_kg_edge_activity_event;
DROP TABLE IF EXISTS kg_edge_activity;
DROP TABLE IF EXISTS project_external_embedding_policy;

ALTER TABLE graph_edges DROP COLUMN last_touched_at;
ALTER TABLE graph_edges DROP COLUMN weight;

DELETE FROM schema_versions WHERE version = 95;

COMMIT;
