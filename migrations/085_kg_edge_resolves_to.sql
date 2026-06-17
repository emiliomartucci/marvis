-- Migration 085 — KG Phase 7.2: add edge type 'resolves_to' (module stub -> file bridge)
--
-- NOTE numerazione: piano originale parla di migration 084 ma 084 e' gia'
-- occupato da `084_drop_legacy_scheduler_tables.sql` (merged on main). Bumpiamo a 085.
--
-- Obiettivo: chiudere il gap "Module <-> File disconnection" identificato dal
-- vision-audit 2026-04-21. Oggi `ast_parser.py` crea due nodi separati per ogni
-- file Python/TS indicizzato:
--   - `py:file:X` (rappresenta il file fisico, contiene funzioni e definizioni)
--   - `py:module:X` (stub target dei "from X import Y" degli altri file)
-- Zero edge tra i due -> `graph_neighbors(py:file:X, direction=incoming)` torna
-- vuoto anche quando il file e' importato, perche' gli import puntano allo
-- stub `py:module:`.
--
-- Fix: introduciamo un nuovo edge type `resolves_to` (stub module -> file
-- canonical). Pattern sweep creera' edge `py:module:X --resolves_to--> py:file:X`
-- per ogni coppia con qualified_name corrispondente. Documentazione +
-- invariante "stub -> canonical sempre" in `kb/knowledge-graph.md`.
--
-- CHANGES:
--   A. CHECK(relation) esteso con 1 valore: `resolves_to` (15 valori totali)
--   B. UNIQUE(source_id, target_id, relation) preservato per idempotent upsert
--   C. Nessun nuovo campo sulle tabelle — solo CHECK constraint extension
--
-- Pattern: drop+recreate identico a 077 (tabella graph_edges rebuild; CHECK
-- constraint extension non supportata da ALTER TABLE in SQLite). Preservo
-- tutti gli indici + sqlite_sequence seed per AUTOINCREMENT.
--
-- Sync enum edge types: da aggiornare in 5 posti (commento cross-reference in
-- ciascuno):
--   1. migrations/085_kg_edge_resolves_to.sql (questa migration, CHECK)
--   2. migrations/073_kg_cross_project.sql   (source-of-truth originale)
--   3. migrations/077_kg_doc_types_extend.sql (mirror rebuild precedente)
--   4. api/routers/graph.py (EDGE_TYPES costante)
--   5. api/services/graph_service.EDGE_TYPES
--   6. mcp-pir/index.mjs edgeTypeEnum
--
-- DEPLOY PROCEDURE (pattern 073/074/077, PAT AM-06):
--   1. Human: `systemctl --user stop pir-kg-watcher.service` (stop watcher per
--      evitare lock contention durante rebuild + sweep bootstrap)
--   2. Human: `systemctl --user stop pir-api.service`
--   3. Human: `sqlite3 /data/pir/console.db "PRAGMA wal_checkpoint(TRUNCATE);"`
--   4. Human: apply migration via api/db.py migration loader
--   5. Human: `systemctl --user start pir-api.service`
--   6. Human: `python3 -m scripts.populate_module_file_bridge`  (one-shot sweep)
--   7. Verify: `SELECT COUNT(*) FROM graph_edges WHERE relation='resolves_to'`
--              -> atteso ~30-4000 edges (cresce col crescere dell'indice)
--   8. Human: `systemctl --user start pir-kg-watcher.service`
--
-- Reversibile: vedi migrations/085_kg_edge_resolves_to_down.sql.
--
-- Dipendenze: migration 084 (drop legacy scheduler tables). Non tocca graph_nodes;
-- solo graph_edges rebuild.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ---- Backup safety net ----------------------------------------------------
DROP TABLE IF EXISTS graph_edges_backup_085;
CREATE TABLE graph_edges_backup_085 AS SELECT * FROM graph_edges;

-- ---- Rebuild graph_edges con CHECK esteso + nuovo edge type ---------------
-- Schema identico a migration 077, CHECK(relation) esteso con 'resolves_to'.
CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',                           -- code (Fase 1a)
        'produces','contains',                                 -- work chain (Fase 1c)
        'describes','documents','cites','applies_to',          -- knowledge chain (Fase 1c)
        'depends_on','mentions','refers_to','shares_tag','similar_to',  -- cross-project (Fase 2)
        'resolves_to'                                          -- Phase 7.2: module stub -> file canonical bridge
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

-- Copia tutti i dati esistenti. Colonne esplicite (PAT C1, data-integrity).
INSERT INTO graph_edges_new (
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until, project_id
)
SELECT
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until, project_id
FROM graph_edges;

DROP TABLE graph_edges;
ALTER TABLE graph_edges_new RENAME TO graph_edges;

-- ---- Preserva sqlite_sequence per AUTOINCREMENT (pattern 077:180-185) -----
DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

-- ---- Indici (ricreati post-rename, mirror 077) ----------------------------
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);

-- Migration 081 (kg_lens_indexes) + ad-hoc partial indexes: ricrea per
-- preservare performance post-rebuild.
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_relation_active
    ON graph_edges(source_id, relation)
    WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_active_target
    ON graph_edges(source_id, relation, target_id) WHERE valid_until IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_active_source
    ON graph_edges(target_id, relation, source_id) WHERE valid_until IS NULL;

-- ---- Schema version bump --------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (85);

COMMIT;

PRAGMA foreign_keys=ON;
