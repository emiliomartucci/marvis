-- Rollback per migration 073 (KG Fase 2 cross-project).
--
-- Strategy: SQLite non supporta DROP COLUMN con CHECK dependency; il rollback
-- qui restituisce lo schema pre-073 usando le tabelle di backup create dalla
-- migration forward. Uso manuale (non chiamato automaticamente): il migration
-- loader e' forward-only.
--
-- Pre-requisiti:
--   - graph_nodes_backup_073 e graph_edges_backup_073 esistono (create dal
--     forward). Se sono stati droppati per liberare spazio, il rollback
--     richiede un restore-from-db-backup esterno.
--
-- Uso:
--   1. Stop pir-api.service
--   2. export KG_HOOK_DISABLED=1
--   3. sqlite3 console.db < migrations/073_rollback.sql
--   4. Restart pir-api.service
--
-- Effetto:
--   - graph_nodes e graph_edges tornano allo schema post-069 (senza project_id,
--     senza nuove relation/type)
--   - schema_versions perde la row version=73
--   - Tutte le nuove edges Fase 2 (depends_on/mentions/refers_to/shares_tag/
--     similar_to) e i nodi type=project vengono cancellati
--
-- ⚠️ DESTRUCTIVE WARNING (review post-deepen):
--   Il rollback restore dai backup_073 → SCARTA tutte le edges Fase 2 create
--   DOPO il deploy della migration. Se passa tempo tra deploy e rollback,
--   si perdono ore/giorni di lavoro populator (mentions/refers_to/cites/
--   shares_tag/similar_to + project nodes + file nodes on-demand).
--
--   PRIMA di eseguire questo rollback, esporta le nuove edges con:
--     sqlite3 console.db <<'EOF' > /tmp/kg-fase2-edges-backup.csv
--       .mode csv
--       .headers on
--       SELECT * FROM graph_edges WHERE relation IN
--         ('depends_on','mentions','refers_to','shares_tag','similar_to');
--       SELECT * FROM graph_nodes WHERE type='project' OR id LIKE 'file:artifact:%';
--     EOF
--   Conserva l'export per re-import manuale post-recovery o re-run populator.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Drop tabelle attuali, ripristina dai backup
DROP TABLE IF EXISTS graph_edges;
DROP TABLE IF EXISTS graph_nodes;

ALTER TABLE graph_nodes_backup_073 RENAME TO graph_nodes;
ALTER TABLE graph_edges_backup_073 RENAME TO graph_edges;

-- Ricrea indici pre-073 (da 065/066/067/068)
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

-- Drop schema version
DELETE FROM schema_versions WHERE version=73;

COMMIT;

PRAGMA foreign_keys=ON;
