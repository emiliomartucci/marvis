-- Migration 069 — KG Fase 1h: estensione CHECK(type) per doc-type distinti
--
-- Aggiunge i tipi knowledge-ext alla colonna `graph_nodes.type`:
--   audit, spike, analysis, research, rubric, guide, mockup
--
-- Questi si affiancano ai tipi esistenti (code/work/knowledge base) senza
-- collassare in un generico 'doc'. Il populate_artifacts.populate_knowledge_docs
-- (Fase 1h) scansiona {metadata_path}/docs/{audits,spikes,analysis,research,
-- rubrics,guides,mockups}/*.md e produce nodi `{type}:artifact:{slug}` per
-- ciascuno. Il NODE_ID_PATTERN viene esteso in tre posti sincronizzati
-- (graph_service.py, scripts/ast_parser.py, mcp-pir/index.mjs).
--
-- Pattern: drop+recreate (mirror di 066). SQLite non supporta MODIFY CHECK su
-- ALTER TABLE, quindi serve la strategia table-rebuild completa.
--
-- Ordine drop/recreate:
--   graph_edges NON viene toccata (nessuna variazione CHECK relation: le
--   relazioni esistenti `describes`/`documents`/`cites`/`applies_to` coprono
--   già i nuovi tipi). Di conseguenza lasciamo le FK intatte e ricostruiamo
--   solo graph_nodes, con FK=OFF per evitare CASCADE a vuoto (sicurezza
--   residua, non strettamente necessaria dato che la tabella target FK
--   è graph_nodes stessa e la rinomina finale riconnette le FK).
--
-- Preserva TUTTE le colonne aggiunte da 065/066/067/068:
--   - base (065/066): id, type, name, qualified_name, file_path, line_number,
--                     metadata, created_at, updated_at
--   - temporal (067): first_seen_git_sha, last_modified_git_sha, last_seen_at,
--                     deprecated_at
--   - touch (068):    touch_count_total, touch_count_7d, touch_count_30d,
--                     touch_authors, touch_last_at
--
-- Reversibile (manual rollback):
--   DROP TABLE graph_nodes;
--   ALTER TABLE graph_nodes_backup_069 RENAME TO graph_nodes;
--   -- (ricreare gli indici da 065/067/068)
--   DELETE FROM schema_versions WHERE version=69;
--
-- Dipendenze: migration 068 (colonne touch).

-- ---- Disable FK PRIMA di BEGIN (mirror di 066, data-integrity C2) ----------
PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ---- Backup safety net -----------------------------------------------------
DROP TABLE IF EXISTS graph_nodes_backup_069;
CREATE TABLE graph_nodes_backup_069 AS SELECT * FROM graph_nodes;

-- ---- graph_nodes: type CHECK esteso (drop+recreate) ------------------------
CREATE TABLE graph_nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'function','file','module',                                      -- code (Fase 1a)
        'task','pr','commit',                                             -- work artifacts (Fase 1c)
        'handoff','solution','learning',                                  -- knowledge base (Fase 1c)
        'audit','spike','analysis','research','rubric','guide','mockup'   -- knowledge ext (Fase 1h)
    )),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    -- 067 temporal columns
    first_seen_git_sha TEXT,
    last_modified_git_sha TEXT,
    last_seen_at TEXT,
    deprecated_at TEXT,
    -- 068 touch columns
    touch_count_total INTEGER NOT NULL DEFAULT 0,
    touch_count_7d INTEGER NOT NULL DEFAULT 0,
    touch_count_30d INTEGER NOT NULL DEFAULT 0,
    touch_authors TEXT NOT NULL DEFAULT '[]',
    touch_last_at TEXT
);

-- Colonne esplicite (mirror di 066: NON SELECT *) — schema-evolution safe.
INSERT INTO graph_nodes_new (
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at
)
SELECT
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at
FROM graph_nodes;

DROP TABLE graph_nodes;
ALTER TABLE graph_nodes_new RENAME TO graph_nodes;

-- ---- Indici (ricreati post-rename, mirror di 065/067/068) ------------------
CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_first_seen ON graph_nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_deprecated ON graph_nodes(deprecated_at)
    WHERE deprecated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d
    ON graph_nodes(touch_count_30d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_7d
    ON graph_nodes(touch_count_7d DESC)
    WHERE type IN ('function','file');

-- ---- Schema version bump ---------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (69);

COMMIT;

-- ---- Re-enable FK fuori dalla transazione (mirror di 066) ------------------
PRAGMA foreign_keys=ON;
