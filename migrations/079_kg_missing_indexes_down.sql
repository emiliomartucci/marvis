-- Down migration 079 — KG Phase 6.5: drop partial indexes added in 079.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_graph_nodes_touch_30d_active;
DROP INDEX IF EXISTS idx_graph_edges_source_relation_active;
DROP INDEX IF EXISTS idx_graph_nodes_project_type_active;

DELETE FROM schema_versions WHERE version = 79;
COMMIT;
