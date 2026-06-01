-- Phase 7.0: composite covering indexes for KG lens subqueries
CREATE INDEX IF NOT EXISTS idx_graph_edges_source_active_target
  ON graph_edges(source_id, relation, target_id) WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_graph_edges_target_active_source
  ON graph_edges(target_id, relation, source_id) WHERE valid_until IS NULL;

CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_lastseen_active
  ON graph_nodes(project_id, last_seen_at DESC) WHERE deprecated_at IS NULL;
