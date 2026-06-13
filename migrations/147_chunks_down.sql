-- Down migration 147 — drop the prose chunks sidecar (Track 2 #4).
-- VERIFY against prod schema_versions max before merge (see 147_chunks.sql header).
DROP INDEX IF EXISTS idx_chunks_doc_id;
DROP TABLE IF EXISTS chunks;
DELETE FROM schema_versions WHERE version = 147;
