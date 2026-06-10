-- 146 (down): no-op.
--
-- The dropped tables were inert dev-time snapshots — full copies of graph_nodes
-- / graph_edges taken before risky KG migrations. They are not restorable from
-- here and nothing depends on them, so the rollback is intentionally empty.
-- run_migrations() never executes *_down.sql (forward-only); this file exists
-- only for manual rollback tooling, which gets a clean no-op.

SELECT 1;
