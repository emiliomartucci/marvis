-- Migration 143 — RI-7 layer 2: per-symbol code embeddings (in-place KG index)
-- Sidecar table for chunk-per-symbol code vectors produced when indexing an
-- arbitrary transmuted repo (`marvis project index <slug>`). Kept SEPARATE from
-- the vec0 `documents` stack (which is doc-file scoped + needs the runtime
-- sqlite-vec extension): code symbols are addressed by their content-addressed
-- KG node id, so a stored BLOB + cosine-in-Python is enough for the OSS local
-- engine and keeps the migration runner extension-free.
-- Reversibile: see 143_kg_code_embeddings_down.sql

CREATE TABLE IF NOT EXISTS graph_node_code_embeddings (
    node_id TEXT PRIMARY KEY,              -- FK to graph_nodes.id (content-addressed)
    project_id TEXT,                       -- scope filter (mirrors graph_nodes.project_id)
    source_file TEXT,                      -- repo-relative path the symbol came from (incremental DELETE key)
    dim INTEGER NOT NULL,                  -- vector length (Granite native = 384)
    vector BLOB NOT NULL,                  -- little-endian float32 packed body embedding
    content_hash TEXT NOT NULL,            -- sha256 of the embedded text (skip re-embed when unchanged)
    model TEXT,                            -- embedding model id (provenance)
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (node_id) REFERENCES graph_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_code_emb_project ON graph_node_code_embeddings(project_id);
CREATE INDEX IF NOT EXISTS idx_code_emb_source_file ON graph_node_code_embeddings(source_file);

INSERT OR IGNORE INTO schema_versions (version) VALUES (143);
