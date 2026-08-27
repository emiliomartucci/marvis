-- Revert migration 166 — restore the mig-136 unqualified documents_fts_update
-- trigger (AFTER UPDATE ON documents). Emergency only: this reinstates the FTS
-- clobber-on-any-update behavior.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS documents_fts_update;
CREATE TRIGGER documents_fts_update AFTER UPDATE ON documents
BEGIN
    DELETE FROM documents_fts WHERE rowid = OLD.id;
    INSERT INTO documents_fts(rowid, doc_id, title, content)
    VALUES (NEW.id, NEW.id, NEW.file_path, NEW.file_path);
END;

COMMIT;

DELETE FROM schema_versions WHERE version = 166;
