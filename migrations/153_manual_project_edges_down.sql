BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_manual_project_edges_dst;
DROP INDEX IF EXISTS idx_manual_project_edges_src;
DROP TABLE IF EXISTS manual_project_edges;
DELETE FROM schema_versions WHERE version = 153;

COMMIT;
