-- Migration 067 — KG Fase 1d: Temporal / freshness tracking
-- v2 post-deepen:
--   - Additive ALTER ADD COLUMN (no table rebuild, preserves FKs and indexes)
--   - BEGIN IMMEDIATE wrap: 7 ALTER + 3 UPDATE + 5 CREATE INDEX without a
--     transaction risks half-migration on crash (each DDL auto-commits).
--   - Colonne ridotte vs v1:
--       * removed `file_modified_at` (YAGNI — duplicato di last_modified_git_sha)
--       * removed `first_seen_git_sha` su edges (derivabile dal source node)
--   - Edges: `valid_until` solo cascade da node deprecation (criterio
--     "non osservato da N run" → Fase 1e touch counter).
--   - Aggiunti indici su `first_seen_at` per time-travel query (data-integrity finding).
--
-- Reversibile (manual rollback): SQLite non supporta DROP COLUMN prima di 3.35
-- (MarvisX usa 3.40+, ma la rimozione richiederebbe un table rebuild — lasciare
-- le colonne NULL è zero-cost, quindi non includiamo un down script).
-- Per rollback forzato: DELETE FROM schema_versions WHERE version=67;
--                       UPDATE graph_nodes SET first_seen_git_sha=NULL, ...;
--
-- Dipendenze: migration 066 (artifact nodes + extended relation CHECK).

BEGIN IMMEDIATE;

-- ---- Nodes: 4 colonne temporali -------------------------------------------
ALTER TABLE graph_nodes ADD COLUMN first_seen_git_sha TEXT;
ALTER TABLE graph_nodes ADD COLUMN last_modified_git_sha TEXT;
ALTER TABLE graph_nodes ADD COLUMN last_seen_at TEXT;
ALTER TABLE graph_nodes ADD COLUMN deprecated_at TEXT;

-- ---- Edges: 3 colonne temporali (no first_seen_git_sha, YAGNI) -------------
ALTER TABLE graph_edges ADD COLUMN first_seen_at TEXT;
ALTER TABLE graph_edges ADD COLUMN last_seen_at TEXT;
ALTER TABLE graph_edges ADD COLUMN valid_until TEXT;

-- ---- Indici ---------------------------------------------------------------
-- Time-travel query: WHERE first_seen_at <= as_of AND (deprecated_at IS NULL OR deprecated_at > as_of)
CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_first_seen ON graph_nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_deprecated ON graph_nodes(deprecated_at)
    WHERE deprecated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;

-- ---- Backfill: sensato baseline per rows pre-esistenti ---------------------
-- last_seen_at = created_at → "è stato visto almeno la prima volta"
UPDATE graph_nodes SET last_seen_at = created_at WHERE last_seen_at IS NULL;
UPDATE graph_edges SET first_seen_at = created_at WHERE first_seen_at IS NULL;
UPDATE graph_edges SET last_seen_at = created_at WHERE last_seen_at IS NULL;

-- ---- Schema version bump ---------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (67);

COMMIT;
