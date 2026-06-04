-- Down migration 143 — drop per-symbol code embeddings sidecar.
DROP INDEX IF EXISTS idx_code_emb_source_file;
DROP INDEX IF EXISTS idx_code_emb_project;
DROP TABLE IF EXISTS graph_node_code_embeddings;
DELETE FROM schema_versions WHERE version = 143;
