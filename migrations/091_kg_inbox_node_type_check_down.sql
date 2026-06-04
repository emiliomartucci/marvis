-- Rollback per migration 091 (KG hotfix — add 'inbox' to graph_nodes.type CHECK).
--
-- Strategy: ripristino via backup tables create dalla forward.
--
-- Pre-requisiti:
--   - graph_nodes_backup_091 e graph_edges_backup_091 esistono (create dal forward).
--
-- Uso:
--   1. Stop pir-api.service
--   2. export KG_HOOK_DISABLED=1
--   3. sqlite3 console.db < migrations/091_kg_inbox_node_type_check_down.sql
--   4. Restart pir-api.service
--
-- DESTRUCTIVE WARNING:
--   Il restore dai backup_091 -> SCARTA i nodi `type='inbox'` creati DOPO il
--   deploy. PRIMA di eseguire questo rollback, esporta i nuovi nodi con:
--     sqlite3 console.db <<'EOF' > /tmp/kg-inbox-nodes-backup.csv
--       .mode csv
--       .headers on
--       SELECT * FROM graph_nodes WHERE type = 'inbox';
--     EOF
--   Inoltre: ricordati di revertire scripts/populate_inbox_nodes.py a v1.0.0
--   (type='artifact') o le run successive falliranno con CHECK constraint.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Drop triggers before DROP TABLE
DROP TRIGGER IF EXISTS graph_nodes_fts_insert;
DROP TRIGGER IF EXISTS graph_nodes_fts_update;
DROP TRIGGER IF EXISTS graph_nodes_fts_delete;
DROP TRIGGER IF EXISTS trg_kg_pins_cleanup;

-- Drop current tables, restore from backup
DROP TABLE IF EXISTS graph_edges;
DROP TABLE IF EXISTS graph_nodes;

ALTER TABLE graph_nodes_backup_091 RENAME TO graph_nodes;
ALTER TABLE graph_edges_backup_091 RENAME TO graph_edges;

-- Ricrea indici (stessa lista della forward)
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
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d_active
    ON graph_nodes(touch_count_30d DESC)
    WHERE deprecated_at IS NULL AND type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_type_active
    ON graph_nodes(project_id, type)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_lastseen_active
    ON graph_nodes(project_id, last_seen_at DESC)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_degree_type
    ON graph_nodes(type, degree DESC)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_metadata_path
    ON graph_nodes(json_extract(metadata, '$.path'))
    WHERE type = 'file' AND deprecated_at IS NULL;

-- Ricrea triggers FTS + kg_pins
CREATE TRIGGER graph_nodes_fts_insert AFTER INSERT ON graph_nodes
WHEN NEW.deprecated_at IS NULL
BEGIN
    INSERT INTO graph_nodes_fts(id, name, qualified_name, file_path, metadata_text, type, project_id)
    VALUES (
        NEW.id,
        NEW.name,
        NEW.qualified_name,
        COALESCE(NEW.file_path, ''),
        COALESCE(NEW.metadata, '{}'),
        NEW.type,
        COALESCE(NEW.project_id, '')
    );
END;

CREATE TRIGGER graph_nodes_fts_update AFTER UPDATE ON graph_nodes
BEGIN
    DELETE FROM graph_nodes_fts WHERE id = OLD.id;
    INSERT INTO graph_nodes_fts(id, name, qualified_name, file_path, metadata_text, type, project_id)
    SELECT
        NEW.id,
        NEW.name,
        NEW.qualified_name,
        COALESCE(NEW.file_path, ''),
        COALESCE(NEW.metadata, '{}'),
        NEW.type,
        COALESCE(NEW.project_id, '')
    WHERE NEW.deprecated_at IS NULL;
END;

CREATE TRIGGER graph_nodes_fts_delete AFTER DELETE ON graph_nodes
BEGIN
    DELETE FROM graph_nodes_fts WHERE id = OLD.id;
END;

CREATE TRIGGER trg_kg_pins_cleanup
AFTER UPDATE OF deprecated_at ON graph_nodes
WHEN NEW.deprecated_at IS NOT NULL
BEGIN
    DELETE FROM kg_pins WHERE node_id = NEW.id;
END;

-- Drop schema version
DELETE FROM schema_versions WHERE version=91;

COMMIT;

PRAGMA foreign_keys=ON;
