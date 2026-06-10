-- Rollback per migration 077 (KG Phase 6 — plan/brainstorm doc types).
--
-- Strategy: SQLite non supporta DROP dal CHECK constraint enum; il rollback
-- ripristina lo schema pre-077 usando le tabelle di backup create dalla
-- migration forward. Uso manuale (non chiamato automaticamente): il migration
-- loader e' forward-only.
--
-- Pre-requisiti:
--   - graph_nodes_backup_077 e graph_edges_backup_077 esistono (create dal
--     forward). Se sono stati droppati, rollback richiede restore-from-db-backup
--     esterno.
--
-- Uso:
--   1. Stop pir-api.service
--   2. export KG_HOOK_DISABLED=1
--   3. sqlite3 console.db < migrations/077_kg_doc_types_extend_down.sql
--   4. Restart pir-api.service
--
-- Effetto:
--   - graph_nodes e graph_edges tornano allo schema post-074 (senza i 2 nuovi
--     types plan|brainstorm nella CHECK)
--   - schema_versions perde la row version=77
--   - Tutte le edges/nodi creati DOPO il deploy della 077 sui types Phase 6
--     vengono persi
--
-- DESTRUCTIVE WARNING:
--   Il restore dai backup_077 -> SCARTA tutti i nodi/edges Phase 6 creati
--   DOPO il deploy della migration. Se passa tempo tra deploy e rollback, si
--   perdono ore/giorni di lavoro populator (plan/brainstorm nodes).
--
--   PRIMA di eseguire questo rollback, esporta i nuovi nodi con:
--     sqlite3 console.db <<'EOF' > /tmp/kg-phase6-nodes-backup.csv
--       .mode csv
--       .headers on
--       SELECT * FROM graph_nodes
--        WHERE type IN ('plan','brainstorm');
--     EOF
--   Conserva l'export per re-import manuale post-recovery o re-run populator.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Drop tabelle attuali, ripristina dai backup
DROP TABLE IF EXISTS graph_edges;
DROP TABLE IF EXISTS graph_nodes;

ALTER TABLE graph_nodes_backup_077 RENAME TO graph_nodes;
ALTER TABLE graph_edges_backup_077 RENAME TO graph_edges;

-- Ricrea indici pre-077 (da 065/066/067/068/073/074)
CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_first_seen ON graph_nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_deprecated ON graph_nodes(deprecated_at)
    WHERE deprecated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d
    ON graph_nodes(touch_count_30d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_7d
    ON graph_nodes(touch_count_7d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project ON graph_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);

-- Drop schema version
DELETE FROM schema_versions WHERE version=77;

COMMIT;

PRAGMA foreign_keys=ON;
