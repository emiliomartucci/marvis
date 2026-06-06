-- Migration 098 — KG hotfix: extend graph_nodes.type CHECK with business document types
--
-- Phase 1.5 E5-fix9 (commit 353b020) ha esteso il lessico Python
--   `_NODE_TYPE_ALLOWLIST` (api/services/ingest/insert_saga.py)
--   `NODE_PREFIXES`        (api/services/graph_service.py)
-- aggiungendo `policy`, `contract`, `transcript`, `report` per file business
-- (policy, contract, transcript, report business-document types).
-- Il CHECK constraint su `graph_nodes.type` (definito in migration 091) NON
-- e' stato esteso, quindi:
--
--   _ensure_kg_edge → INSERT OR IGNORE INTO graph_nodes (..., type='policy', ...)
--                  → silently skipped per CHECK fail
--                  → poi INSERT INTO graph_edges (source=project, target=policy_node)
--                  → sqlite3.IntegrityError: FOREIGN KEY constraint failed
--                    (target_id non esiste in graph_nodes)
--
-- Saga rotta per ogni file LLM-classified come policy/contract/transcript/report.
-- Test live: two business-document files (policy + scoped-access doc)
-- → status=inserted ma KG non aggiornato + embedding non indexed.
--
-- Pattern: stesso di 091 (drop+recreate table, SQLite non supporta ALTER TABLE
-- MODIFY CHECK).
--
-- DEPLOY PROCEDURE (manuale, non auto-applicata):
--   1. Human: `systemctl --user stop pir-api.service`
--   2. Human: `sqlite3 /data/pir/console.db "PRAGMA wal_checkpoint(TRUNCATE);"`
--   3. Human: `sqlite3 /data/pir/console.db < migrations/098_kg_node_type_business.sql`
--   4. Verify: `sqlite3 /data/pir/console.db "SELECT version FROM schema_versions ORDER BY version DESC LIMIT 1;"` → 98
--   5. Human: `systemctl --user start pir-api.service`
--   6. Re-test: drop a policy file → saga completa → graph_nodes contains
--      'policy:artifact:<project>/docs/policies/<file>' with type='policy'
--
-- Reversibile: vedi migrations/098_kg_node_type_business_down.sql.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ---- Backup safety net ----------------------------------------------------
DROP TABLE IF EXISTS graph_nodes_backup_098;
DROP TABLE IF EXISTS graph_edges_backup_098;
CREATE TABLE graph_nodes_backup_098 AS SELECT * FROM graph_nodes;
CREATE TABLE graph_edges_backup_098 AS SELECT * FROM graph_edges;

-- IMPORTANTE: graph_edges ha FK ON DELETE CASCADE verso graph_nodes.
-- Rebuild graph_edges PRIMA, poi graph_nodes (mirror 091).

-- ---- Drop triggers prima di DROP TABLE ------------------------------------
DROP TRIGGER IF EXISTS graph_nodes_fts_insert;
DROP TRIGGER IF EXISTS graph_nodes_fts_update;
DROP TRIGGER IF EXISTS graph_nodes_fts_delete;
DROP TRIGGER IF EXISTS trg_kg_pins_cleanup;

-- ---- graph_edges: rebuild consistency (PAT AM-02) — CHECK invariato -------
CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',                           -- code (Fase 1a)
        'produces','contains',                                 -- work chain (Fase 1c)
        'describes','documents','cites','applies_to',          -- knowledge chain (Fase 1c)
        'depends_on','mentions','refers_to','shares_tag','similar_to',  -- cross-project (Fase 2)
        'resolves_to'                                          -- bridge stub->file (Phase 7.2, mig 085)
    )),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    source TEXT NOT NULL DEFAULT 'ast' CHECK(source IN (
        'ast','git','db','frontmatter','rem','llm','manual'
    )),
    metadata TEXT NOT NULL DEFAULT '{}',
    source_file TEXT,
    source_line INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    first_seen_at TEXT,
    last_seen_at TEXT,
    valid_until TEXT,
    project_id TEXT,
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);

INSERT INTO graph_edges_new (
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until,
    project_id
)
SELECT
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until,
    project_id
FROM graph_edges;

DROP TABLE graph_edges;
ALTER TABLE graph_edges_new RENAME TO graph_edges;

-- ---- graph_nodes: type CHECK esteso con business doc types ----------------
CREATE TABLE graph_nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'function','file','module',                                      -- code (Fase 1a)
        'task','pr','commit',                                             -- work artifacts (Fase 1c)
        'handoff','solution','learning',                                  -- knowledge base (Fase 1c)
        'audit','spike','analysis','research','rubric','guide','mockup', -- knowledge ext (Fase 1h)
        'project',                                                        -- cross-project node (Fase 2)
        'hook','skill','command','plugin',                                -- infra-indexing (Fase 2.z)
        'plan','brainstorm',                                              -- knowledge ext (Phase 6)
        'inbox',                                                          -- inbox saved items (hotfix 091)
        'policy','contract','transcript','report'                         -- business documents (E5-fix9, mig 098)
    )),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 067 temporal
    first_seen_git_sha TEXT,
    last_modified_git_sha TEXT,
    last_seen_at TEXT,
    deprecated_at TEXT,
    -- 068 touch
    touch_count_total INTEGER NOT NULL DEFAULT 0,
    touch_count_7d INTEGER NOT NULL DEFAULT 0,
    touch_count_30d INTEGER NOT NULL DEFAULT 0,
    touch_authors TEXT NOT NULL DEFAULT '[]',
    touch_last_at TEXT,
    -- 073 cross-project
    project_id TEXT,
    -- 085 degree
    degree INTEGER NOT NULL DEFAULT 0
);

INSERT INTO graph_nodes_new (
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at,
    project_id,
    degree
)
SELECT
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at,
    project_id,
    degree
FROM graph_nodes;

DROP TABLE graph_nodes;
ALTER TABLE graph_nodes_new RENAME TO graph_nodes;

-- Preserva sqlite_sequence per AUTOINCREMENT graph_edges.id (PAT AM-01).
DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

-- ---- Indici (ricreati post-rename, mirror live schema) --------------------
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

-- ---- Ricrea triggers FTS + kg_pins (mirror 091) ---------------------------
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

-- ---- Schema version bump ---------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (98);

COMMIT;

PRAGMA foreign_keys=ON;
