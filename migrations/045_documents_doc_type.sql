-- Migration 045: Add doc_type, doc_title, workspace_id to documents
-- Columns added via Python hook in db.py (_add_documents_columns) AFTER this SQL runs.
-- Index created in Python hook after column exists.

INSERT OR IGNORE INTO schema_versions (version) VALUES (45);
