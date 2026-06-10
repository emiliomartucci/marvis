-- v1.1.0 - 2026-04-17 - degree column + indexes (backfill via separate script)
-- NOTE: uses column `type` (actual graph_nodes schema), not `kind` (which does not exist).
-- NOTE: graph_edges uses source_id/target_id (not source/target).
-- Backfill moved to scripts/backfill_graph_nodes_degree.py (was too slow for 15s startup timeout).
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

ALTER TABLE graph_nodes ADD COLUMN degree INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_graph_nodes_degree_type
  ON graph_nodes(type, degree DESC)
  WHERE deprecated_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_graph_nodes_metadata_path
  ON graph_nodes(json_extract(metadata, '$.path'))
  WHERE type = 'file' AND deprecated_at IS NULL;

INSERT OR IGNORE INTO schema_versions(version) VALUES (83);
COMMIT;
PRAGMA foreign_keys=ON;
