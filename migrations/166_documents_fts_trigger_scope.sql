-- Migration 166 — scope documents_fts_update trigger to content columns
--
-- The mig-136 trigger was `AFTER UPDATE ON documents` (UNQUALIFIED): ANY UPDATE
-- (salience, archived, confidential, content_hash) rebuilt the FTS row as
-- path-only (content = file_path), destroying the rich full-text body that the
-- Python backfill (_backfill_documents_fts) populates. A bulk salience UPDATE
-- (brain salience-decay phase) thus clobbered the FTS content of tens of
-- thousands of documents in one cycle -> BM25 lane matched on paths only ->
-- search ranking regression (incident 2026-07-04, learning ca377727).
--
-- Fix: scope the trigger to `OF file_path, doc_title` so only a path/title
-- change re-touches the FTS row. Metadata-only updates (salience, archived,
-- confidential, content_hash, ...) no longer fire it and no longer clobber the
-- rich content. This also removes the latent per-row degradation caused by
-- boost_document / update_salience / mark_confidential.
--
-- Safe: search excludes archived AND confidential docs at QUERY time
-- (embedding_service._fetch_document_rows: `COALESCE(archived,0)=0` + the RBAC-F4
-- confidential mirror), independent of whether a row lingers in documents_fts —
-- so not refreshing the FTS on those metadata changes cannot leak them.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS documents_fts_update;
CREATE TRIGGER documents_fts_update AFTER UPDATE OF file_path, doc_title ON documents
BEGIN
    DELETE FROM documents_fts WHERE rowid = OLD.id;
    INSERT INTO documents_fts(rowid, doc_id, title, content)
    VALUES (NEW.id, NEW.id, NEW.file_path, NEW.file_path);
END;

COMMIT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (166);
