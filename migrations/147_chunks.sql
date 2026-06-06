-- Migration 147 — Track 2 #4: fixed-size prose chunks (flag MARVIS_CHUNKING)
--
-- VERIFY against prod schema_versions max before merge: this file is numbered
-- 147 from the local `ls migrations/` (last applied = 146). If prod has advanced
-- past 146 by the time this lands, renumber to (prod max + 1) — the orchestrator
-- will confirm. The migration is ADDITIVE (CREATE TABLE IF NOT EXISTS + index
-- only); it touches no existing row, so it is safe to apply even with the
-- MARVIS_CHUNKING flag OFF (table stays empty, live behavior unchanged).
--
-- Sidecar table holding per-chunk vectors for PROSE documents when chunking is
-- enabled. Kept SEPARATE from the vec0 `vec_documents` stack (1 vector per doc):
-- a doc fans out to N chunk rows here, each carrying the byte span (span_start /
-- span_end into the original UTF-8 text) the #2 span-citation layer needs, plus a
-- per-chunk content_hash for incremental re-embed. Vector stored as a packed BLOB
-- (little-endian float32, mirrors graph_node_code_embeddings) so the migration
-- runner stays sqlite-vec-extension-free; ANN search keys on chunk_id at query
-- time. doc_id is the parent `documents.id` so search can MAX-POOL chunk->doc.
-- Reversibile: see 147_chunks_down.sql

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id TEXT PRIMARY KEY,              -- stable id: <doc_id>:<chunk_idx>
    doc_id TEXT,                            -- parent documents.id (MAX-POOL group key)
    chunk_idx INTEGER,                      -- ordinal within the doc (0-based)
    span_start INTEGER,                     -- UTF-8 BYTE offset into original text (inclusive)
    span_end INTEGER,                       -- UTF-8 BYTE offset into original text (exclusive)
    content_hash TEXT,                      -- sha256 of the chunk byte span (incremental re-embed)
    vector BLOB                             -- little-endian float32 packed chunk embedding
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc_id ON chunks(doc_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (147);
