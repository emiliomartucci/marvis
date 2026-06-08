-- Migration 077 — KG Phase 6: estensione CHECK(type) per plan/brainstorm doc types
--
-- Aggiunge i 2 doc-type mancanti alla colonna `graph_nodes.type`:
--   plan, brainstorm
--
-- Questi si affiancano ai 8 doc-types knowledge-ext gia' presenti (audit/spike/
-- analysis/research/rubric/guide/mockup introdotti dalla 069, solution dalla
-- baseline 065). Il populate_knowledge_docs (Phase 6) scansiona
-- {metadata_path}/docs/{plans,brainstorms}/*.md e produce nodi
-- `{type}:artifact:{slug}` per ciascuno. Il NODE_ID_PATTERN viene esteso in
-- tre posti sincronizzati (graph_service.py, scripts/ast_parser.py,
-- mcp-pir/index.mjs).
--
-- Phase 6 context (session 140, docs/solutions/2026-04-16-kg-phase5-verification.md):
-- la baseline KG ha rivelato coverage 18% handoff (128/709) — solo marvisx veniva
-- indicizzato. 67 progetti metadata-only (1billion, c&i-tool, oltrepocase...)
-- contengono handoff/plan/brainstorm/solution mai visti dal KG. Questa migration
-- estende il CHECK in modo che `populate_artifacts --all-projects` possa creare
-- nodi `plan` e `brainstorm` per tutti i 68 progetti.
--
-- Pattern: drop+recreate (mirror 066/069/073/074). SQLite NON supporta MODIFY
-- CHECK via ALTER TABLE. graph_edges ricostruita PRIMA di graph_nodes per
-- evitare CASCADE a vuoto dei vincoli FK.
--
-- CHANGES:
--   A. CHECK(type) esteso con `plan|brainstorm` (2 nuovi doc-type)
--   B. Nessuna nuova colonna — solo estensione CHECK constraint
--   C. Rebuild ANCHE graph_edges per pattern consistency (PAT AM-02, mirror 074)
--      nessun CHECK(relation) change ma rebuild mirror 073/074 per simmetria
--      lifecycle.
--   D. Preserve sqlite_sequence (PAT AM-01, copia 074 lines 159-167)
--
-- DEPLOY PROCEDURE (pattern 073/074, PAT AM-06):
--   1. Human: `export KG_HOOK_DISABLED=1` (stop post-commit hook scritture sul DB)
--   2. Human: `systemctl --user stop pir-api.service` (rilascia read-only lock)
--   3. Human: `sqlite3 /data/pir/console.db "PRAGMA wal_checkpoint(TRUNCATE);"`
--   4. Human: apply migration via api/db.py migration loader (start pir-api.service)
--   5. Verify: `SELECT count(*) FROM graph_nodes WHERE project_id IS NULL` → 0
--   6. Human: `python -m scripts.populate_artifacts --all-projects`
--   7. Human: `python -m scripts.populate_cross_project --include-all-projects`
--   8. Human: `unset KG_HOOK_DISABLED`
--
-- Preserva TUTTE le colonne aggiunte da 065/066/067/068/069/073/074:
--   - base (065): id, type, name, qualified_name, file_path, line_number,
--                 metadata, created_at, updated_at
--   - temporal (067): first_seen_git_sha, last_modified_git_sha, last_seen_at,
--                     deprecated_at
--   - touch (068):    touch_count_total, touch_count_7d, touch_count_30d,
--                     touch_authors, touch_last_at
--   - cross-project (073): project_id
--   - infra-types (074): nessuna colonna nuova, solo CHECK extension
--
-- Reversibile: vedi migrations/077_kg_doc_types_extend_down.sql (restore da backup_077).
--
-- Dipendenze: migration 074 (infra-types extension).

-- ---- Disable FK PRIMA di BEGIN (mirror di 066, data-integrity C2) ----------
-- PRAGMA foreign_keys dentro una tx e' no-op silente in SQLite.
PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ---- Backup safety net (data-integrity C4) ---------------------------------
-- Nota: non tocchiamo `graph_nodes_backup_069` ne' `graph_nodes_backup_073`
-- ne' `graph_nodes_backup_074`: sono backup storici conservati come baseline
-- di rollback delle singole migration precedenti.
DROP TABLE IF EXISTS graph_nodes_backup_077;
DROP TABLE IF EXISTS graph_edges_backup_077;
CREATE TABLE graph_nodes_backup_077 AS SELECT * FROM graph_nodes;
CREATE TABLE graph_edges_backup_077 AS SELECT * FROM graph_edges;

-- IMPORTANTE: ordine DROP. graph_edges ha FK ON DELETE CASCADE verso
-- graph_nodes. Ricostruiamo graph_edges PRIMA, poi graph_nodes.

-- ---- graph_edges: rebuild consistency (PAT AM-02) — CHECK(relation) invariato
CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',                           -- code (Fase 1a)
        'produces','contains',                                 -- work chain (Fase 1c)
        'describes','documents','cites','applies_to',          -- knowledge chain (Fase 1c)
        'depends_on','mentions','refers_to','shares_tag','similar_to'  -- cross-project (Fase 2)
    )),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    source TEXT NOT NULL DEFAULT 'ast' CHECK(source IN (
        'ast','git','db','frontmatter','rem','llm','manual'
    )),
    metadata TEXT NOT NULL DEFAULT '{}',
    source_file TEXT,
    source_line INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 067 temporal columns preserved
    first_seen_at TEXT,
    last_seen_at TEXT,
    valid_until TEXT,
    -- 073 cross-project column
    project_id TEXT,
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);

-- Colonne esplicite (data-integrity C1, NON SELECT *).
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

-- ---- graph_nodes: type CHECK esteso con plan/brainstorm (Phase 6) ---------
CREATE TABLE graph_nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'function','file','module',                                      -- code (Fase 1a)
        'task','pr','commit',                                             -- work artifacts (Fase 1c)
        'handoff','solution','learning',                                  -- knowledge base (Fase 1c)
        'audit','spike','analysis','research','rubric','guide','mockup', -- knowledge ext (Fase 1h)
        'project',                                                        -- cross-project node (Fase 2)
        'hook','skill','command','plugin',                                -- infra-indexing (Fase 2.z)
        'plan','brainstorm'                                               -- knowledge ext (Phase 6)
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
    project_id TEXT
);

-- Colonne esplicite (C1) — preserva project_id backfilled da 073.
INSERT INTO graph_nodes_new (
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at,
    project_id
)
SELECT
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at,
    project_id
FROM graph_nodes;

DROP TABLE graph_nodes;
ALTER TABLE graph_nodes_new RENAME TO graph_nodes;

-- Preserva sqlite_sequence per AUTOINCREMENT graph_edges.id (PAT AM-01, mirror 074 linee 159-167).
-- Note: DROP TABLE puo' lasciare row stale in sqlite_sequence; cleanup esplicito
-- prima di re-insert per evitare duplicati sotto re-run/migration chain.
DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

-- ---- Indici (ricreati post-rename, mirror di 065/066/067/068/069/073/074) --
-- Base
CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
-- 066 edge indices
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);
-- 067 temporal
CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_first_seen ON graph_nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_deprecated ON graph_nodes(deprecated_at)
    WHERE deprecated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
-- 068 touch
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d
    ON graph_nodes(touch_count_30d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_7d
    ON graph_nodes(touch_count_7d DESC)
    WHERE type IN ('function','file');
-- 073 cross-project
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project ON graph_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);

-- ---- Sanity: project_id backfill (forza safety, no-op se gia' a posto) -----
UPDATE graph_nodes SET project_id='marvisx' WHERE project_id IS NULL;
UPDATE graph_edges SET project_id='marvisx' WHERE project_id IS NULL;

-- ---- Schema version bump ---------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (77);

COMMIT;

-- ---- Re-enable FK fuori dalla transazione (mirror 066/073/074) -------------
PRAGMA foreign_keys=ON;
