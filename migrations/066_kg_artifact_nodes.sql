-- Migration 066 — KG Fase 1c: Artefatti come nodi (task/PR/commit/handoff/solution/learning)
-- v2 post-deepen (data-integrity findings applied):
--   C1: colonne esplicite negli INSERT (no SELECT *) — schema-evolution safe
--   C2: PRAGMA foreign_keys=OFF PRIMA di BEGIN — DROP TABLE triggera CASCADE su
--       graph_edges (FK ON DELETE CASCADE verso graph_nodes). Senza disable,
--       DROP TABLE graph_nodes svuoterebbe graph_edges. defer_foreign_keys NON
--       basta perche' DROP non è un constraint check, e PRAGMA foreign_keys
--       dentro una transazione è no-op silente.
--   C4: backup table pre-drop (graph_nodes_backup_066, graph_edges_backup_066)
--   sqlite_sequence preservato per AUTOINCREMENT graph_edges.id
-- Reversibile (manual rollback, su DB con FK=OFF):
--   DROP TABLE graph_nodes; DROP TABLE graph_edges;
--   ALTER TABLE graph_nodes_backup_066 RENAME TO graph_nodes;
--   ALTER TABLE graph_edges_backup_066 RENAME TO graph_edges;
--   DELETE FROM schema_versions WHERE version=66;

-- ---- Disable FK PRIMA di BEGIN (data-integrity C2) -------------------------
-- DEVE essere fuori dalla transazione: `PRAGMA foreign_keys` dentro a una tx
-- è no-op silente in SQLite.
PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ---- Backup safety net (data-integrity C4) ---------------------------------
-- Drop existing backup tables from any prior failed apply so re-run è clean.
DROP TABLE IF EXISTS graph_nodes_backup_066;
DROP TABLE IF EXISTS graph_edges_backup_066;
CREATE TABLE graph_nodes_backup_066 AS SELECT * FROM graph_nodes;
CREATE TABLE graph_edges_backup_066 AS SELECT * FROM graph_edges;

-- IMPORTANTE: l'ordine DROP è fondamentale.
-- graph_edges ha FK ON DELETE CASCADE verso graph_nodes. Se droppassimo
-- graph_nodes per primo (con FK enforcement attivo, non sospendibile dentro
-- una transazione tramite foreign_keys=OFF), il CASCADE cancellerebbe TUTTE
-- le righe di graph_edges prima di poter copiare. Quindi:
--   1. ricostruisci graph_edges per primo (così non ha più FK pendenti
--      verso una graph_nodes che sta per essere droppata)
--   2. ricostruisci graph_nodes per secondo
-- Le nuove FK verranno create al RENAME finale di graph_edges_new.

-- ---- graph_edges: relation CHECK esteso (RICOSTRUITA PER PRIMA) ------------
CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',                          -- code
        'produces','contains',                                 -- work chain (touches deferred to 1d)
        'describes','documents','cites','applies_to'           -- knowledge chain
    )),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    source TEXT NOT NULL DEFAULT 'ast' CHECK(source IN (
        'ast','git','db','frontmatter','rem','llm','manual'
    )),
    metadata TEXT NOT NULL DEFAULT '{}',
    source_file TEXT,
    source_line INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);

-- Colonne esplicite (data-integrity C1)
INSERT INTO graph_edges_new (
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at
)
SELECT
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at
FROM graph_edges;

DROP TABLE graph_edges;
ALTER TABLE graph_edges_new RENAME TO graph_edges;

-- ---- graph_nodes: type CHECK esteso (RICOSTRUITA PER SECONDA) --------------
CREATE TABLE graph_nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'function','file','module',         -- code (Fase 1a)
        'task','pr','commit',                -- work artifacts
        'handoff','solution','learning'      -- knowledge artifacts (no audit in 1c, YAGNI)
    )),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Colonne esplicite (data-integrity C1, NON SELECT *)
INSERT INTO graph_nodes_new (
    id, type, name, qualified_name, file_path, line_number,
    metadata, created_at, updated_at
)
SELECT
    id, type, name, qualified_name, file_path, line_number,
    metadata, created_at, updated_at
FROM graph_nodes;

DROP TABLE graph_nodes;
ALTER TABLE graph_nodes_new RENAME TO graph_nodes;

-- Preserva sqlite_sequence per AUTOINCREMENT graph_edges.id
-- (DROP+RENAME perde la sequenza altrimenti). UPSERT manuale: la riga esiste
-- solo se la table aveva mai inserito record AUTOINCREMENT prima.
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges
WHERE NOT EXISTS (SELECT 1 FROM sqlite_sequence WHERE name='graph_edges');
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

-- ---- Indici (idempotenti, ricreati post-rename) ----------------------------
CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);

-- ---- Schema version bump ---------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (66);

COMMIT;

-- ---- Re-enable FK fuori dalla transazione (mirror del PRAGMA pre-BEGIN) ----
PRAGMA foreign_keys=ON;
