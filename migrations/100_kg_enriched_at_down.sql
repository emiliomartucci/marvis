-- Rollback per migration 100 (kg_enriched_at column).
--
-- SQLite < 3.35 non supporta ALTER TABLE DROP COLUMN. Rollback canonico
-- richiede drop+recreate graph_nodes (heavy). Per rollback minimo basta
-- rimuovere index + version bump; la colonna resta ma diventa orphan.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_graph_nodes_enriched_pending;
DELETE FROM schema_versions WHERE version = 100;

COMMIT;
