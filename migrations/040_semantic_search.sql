-- Migration 040: Semantic search documents table
-- Feature: Handoff embedding search (task dc729190)
-- Note: vec0 virtual table created at RUNTIME (migration runner can't load extensions)
-- Schema: vec_documents(doc_id INTEGER PK, embedding float[512])

CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY,
    file_path TEXT NOT NULL UNIQUE,
    project TEXT NOT NULL,
    content_hash TEXT,
    created_at TEXT DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_documents_project ON documents(project);

INSERT OR IGNORE INTO schema_versions (version) VALUES (40);
