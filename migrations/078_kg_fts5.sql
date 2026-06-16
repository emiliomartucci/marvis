-- Migration 078 — KG Phase 6.5: FTS5 virtual table for hybrid search
--
-- Problem (pre-Phase 6.5): kg_full_text_search runs `LOWER(name) LIKE '%q%'`
-- against graph_nodes. This is O(n), leaks SQL metacharacters (%, _), and
-- blocks BM25 scoring. For ~30k nodes and growing the probe latency is
-- unacceptable.
--
-- Solution: introduce `graph_nodes_fts`, an FTS5 virtual table with tokenizer
-- `unicode61 remove_diacritics 2` so Italian queries (`iperammortamento`,
-- `perche`, `citta`) match correctly regardless of accent normalization.
-- Triggers keep it in sync with INSERT / UPDATE / DELETE on graph_nodes.
-- Data is duplicated into FTS columns (NOT contentless) so callers can
-- read id/type/project_id directly from MATCH queries without a JOIN.
-- At ~30k rows the storage overhead is ~10MB, acceptable for the
-- hybrid-search latency win.
--
-- Contract with callers (api/services/search.py): MATCH queries return
-- graph_nodes.id plus bm25() score. See kg_full_text_search() for the
-- hybrid retrieval pipeline that composes this with the semantic retriever.
--
-- Deploy:
--   1. stop pir-api.service (WAL lock released)
--   2. apply migration via api/db.py loader
--   3. the initial INSERT SELECT populates from active graph_nodes
--   4. triggers keep it current; subsequent populate_* runs stay in sync
--
-- v0.0.0 - 2026-04-16 - KG Phase 6.5 A (hybrid search + FTS5 retriever)

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE VIRTUAL TABLE IF NOT EXISTS graph_nodes_fts USING fts5(
    id UNINDEXED,
    name,
    qualified_name,
    file_path,
    metadata_text,
    type UNINDEXED,
    project_id UNINDEXED,
    tokenize='unicode61 remove_diacritics 2'
);

-- Populate from currently active graph_nodes (exclude deprecated to keep
-- the index lean; triggers will add them back on re-activation).
INSERT INTO graph_nodes_fts(id, name, qualified_name, file_path, metadata_text, type, project_id)
SELECT
    id,
    name,
    qualified_name,
    COALESCE(file_path, ''),
    COALESCE(metadata, '{}'),
    type,
    COALESCE(project_id, '')
FROM graph_nodes
WHERE deprecated_at IS NULL;

-- Triggers: full synchronization with graph_nodes lifecycle.
-- On INSERT we also exclude rows that are already deprecated (should be
-- rare — mostly historical backfill). On UPDATE we delete+insert so the
-- tokenizer re-runs when name / metadata change.

DROP TRIGGER IF EXISTS graph_nodes_fts_insert;
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

DROP TRIGGER IF EXISTS graph_nodes_fts_update;
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

DROP TRIGGER IF EXISTS graph_nodes_fts_delete;
CREATE TRIGGER graph_nodes_fts_delete AFTER DELETE ON graph_nodes
BEGIN
    DELETE FROM graph_nodes_fts WHERE id = OLD.id;
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (78);
COMMIT;
PRAGMA foreign_keys=ON;
