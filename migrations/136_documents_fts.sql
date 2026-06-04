-- Migration 136 — documents_fts for W2 hybrid retrieval
--
-- Adds an FTS5 index over the semantic-search documents table so
-- embedding_service.search_by_type can fuse sqlite-vec KNN with BM25 over
-- handoffs, files, audits, learnings, tasks, inbox items, and projects.
--
-- We intentionally keep the FTS table content-owned instead of
-- content='documents': the live documents table has doc_title/file_path but
-- no title/content body columns, and an external-content FTS5 table with
-- non-existent source columns breaks ordinary reads and rebuilds. The Python
-- migration post-hook backfills full file/row bodies where possible.

BEGIN IMMEDIATE;

CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    doc_id UNINDEXED,
    title,
    content,
    tokenize='porter unicode61 remove_diacritics 2'
);

-- SQL-only fallback backfill. The migration runner post-hook replaces these
-- lightweight rows with full readable body text for loadable documents.
INSERT INTO documents_fts(rowid, doc_id, title, content)
SELECT d.id, d.id, d.file_path, d.file_path
FROM documents AS d
WHERE NOT EXISTS (
    SELECT 1
    FROM documents_fts AS f
    WHERE f.rowid = d.id
);

DROP TRIGGER IF EXISTS documents_fts_insert;
CREATE TRIGGER documents_fts_insert AFTER INSERT ON documents
BEGIN
    INSERT INTO documents_fts(rowid, doc_id, title, content)
    VALUES (NEW.id, NEW.id, NEW.file_path, NEW.file_path);
END;

DROP TRIGGER IF EXISTS documents_fts_update;
CREATE TRIGGER documents_fts_update AFTER UPDATE ON documents
BEGIN
    DELETE FROM documents_fts WHERE rowid = OLD.id;
    INSERT INTO documents_fts(rowid, doc_id, title, content)
    VALUES (NEW.id, NEW.id, NEW.file_path, NEW.file_path);
END;

DROP TRIGGER IF EXISTS documents_fts_delete;
CREATE TRIGGER documents_fts_delete AFTER DELETE ON documents
BEGIN
    DELETE FROM documents_fts WHERE rowid = OLD.id;
END;

COMMIT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (136);
