-- Migration 135 — KG edge provider tracking
--
-- Adds a nullable provider field for newly written KG edges. The live KG edge
-- table is named `graph_edges`; this migration keeps the task wording
-- `kg_edges_provider` in the filename for traceability.
--
-- No retroactive backfill is performed in this migration. Legacy `similar_to`
-- rows stay NULL until they are naturally recomputed by the populator during
-- the transition window.
--
-- Idempotency note:
-- SQLite does not support `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`. The
-- guarded column add and index creation run in the version-135 post-migration
-- hook in `core/api/db.py`. This SQL file only reserves/stamps the version so
-- startup migration ordering remains deterministic.

BEGIN IMMEDIATE;

INSERT OR IGNORE INTO schema_versions(version) VALUES (135);

COMMIT;
