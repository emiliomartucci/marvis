-- Rollback per migration 085 (KG Phase 7.2 — resolves_to edge type).
--
-- Strategy: SQLite non supporta DROP da CHECK constraint enum; rollback
-- ripristina lo schema pre-085 usando la tabella di backup creata dalla
-- migration forward. Uso manuale (migration loader e' forward-only).
--
-- DESTRUCTIVE WARNING:
--   Il restore da graph_edges_backup_085 NON riporta in vita eventuali edge
--   `resolves_to` creati DOPO il deploy della 085 — se sono state eseguite
--   corse del populator `populate_module_file_bridge`, quei dati vengono
--   PERSI al rollback. Prima di rollback, export suggerito:
--     sqlite3 console.db <<'EOF' > /tmp/kg-phase72-resolves-backup.csv
--       .mode csv
--       .headers on
--       SELECT * FROM graph_edges WHERE relation='resolves_to';
--     EOF
--
-- Pre-requisiti:
--   - graph_edges_backup_085 deve esistere (creata dal forward). Se droppata,
--     rollback richiede restore-from-db-backup esterno.
--
-- Uso:
--   1. systemctl --user stop pir-kg-watcher.service
--   2. systemctl --user stop pir-api.service
--   3. sqlite3 console.db < migrations/085_kg_edge_resolves_to_down.sql
--   4. systemctl --user start pir-api.service
--   5. systemctl --user start pir-kg-watcher.service
--
-- Effetto:
--   - graph_edges torna allo schema post-077/081 (CHECK con 14 valori, senza
--     'resolves_to')
--   - schema_versions perde la row version=85
--   - Eventuali edge 'resolves_to' vengono rimossi dal DB

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Drop current graph_edges + restore from backup.
DROP TABLE IF EXISTS graph_edges;
ALTER TABLE graph_edges_backup_085 RENAME TO graph_edges;

-- Ricrea indici pre-085 (mirror 077/081)
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_relation_active
    ON graph_edges(source_id, relation)
    WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_active_target
    ON graph_edges(source_id, relation, target_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_active_source
    ON graph_edges(target_id, relation, source_id) WHERE valid_until IS NULL;

-- Drop schema version
DELETE FROM schema_versions WHERE version=85;

COMMIT;

PRAGMA foreign_keys=ON;
