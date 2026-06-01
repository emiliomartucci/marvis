-- Migration 079 — KG Phase 6.5: missing indexes for hybrid search + coverage report
--
-- Perf oracle review (deepen-plan 2026-04-16):
--   - graph_hotspots scans `touch_count_30d DESC` with type IN ('function','file')
--     and deprecated_at IS NULL; today only the unpartial index from 068
--     covers the sort — this partial mirror trims it to the hot subset.
--   - find_edge_path batch query in hybrid search does
--     WHERE source_id IN (...) AND relation='describes' AND valid_until IS NULL
--   - kg_coverage_report groups by (project_id, type) over active nodes.
--
-- v0.0.0 - 2026-04-16 - KG Phase 6.5 A/B (hybrid search + coverage report)

BEGIN IMMEDIATE;

-- Hotspots query: narrow to active function/file by churn.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d_active
    ON graph_nodes(touch_count_30d DESC)
    WHERE deprecated_at IS NULL AND type IN ('function','file');

-- Batch edge_path lookup during hybrid fusion.
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_relation_active
    ON graph_edges(source_id, relation)
    WHERE valid_until IS NULL;

-- kg_coverage_report GROUP BY project_id, type.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_type_active
    ON graph_nodes(project_id, type)
    WHERE deprecated_at IS NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (79);
COMMIT;
