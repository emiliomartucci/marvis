-- v1.0.0 - 2026-04-17 - Rollback migration 083 (graph_nodes.degree + indexes)
-- For dev/test only. Production rollback = restore backup (api/db.py keeps last 3).
-- NOTE: SQLite does not support DROP COLUMN before 3.35.0 (2021-03-12).
-- On SQLite >= 3.35.0 (production uses 3.45.1) this is safe.
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_graph_nodes_metadata_path;
DROP INDEX IF EXISTS idx_graph_nodes_degree_type;
ALTER TABLE graph_nodes DROP COLUMN degree;

DELETE FROM schema_versions WHERE version = 83;

COMMIT;
PRAGMA foreign_keys=ON;
