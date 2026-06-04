-- 146: drop the stale graph_{nodes,edges}_backup_NNN snapshot tables.
--
-- Migrations 066/069/073/074/077/085/091/098/125/132 each took a one-off
-- `CREATE TABLE graph_nodes_backup_NNN AS SELECT * FROM graph_nodes` (and the
-- graph_edges equivalent) as a dev-time safety snapshot before a risky KG
-- rewrite. Those 17 snapshots are inert yet ship in every fresh `marvis init`
-- DB (~13% of the file) — a "raw schema" smell for anyone evaluating the OSS.
--
-- Safe to drop here:
--   * No runtime code (Python/SQL/TS) reads graph_*_backup_*; they are dead
--     copies. The only forward reader was migration 099 (restoring edge weights
--     from graph_edges_backup_098), which runs at version 99 — far before this
--     one — so the backup has already served its purpose.
--   * run_migrations() is forward-only (it skips *_down.sql), so the historical
--     down-migrations that RENAME from these backups never execute on a real DB.
--   * DROP ... IF EXISTS is idempotent.
-- Reversibile: see 146_drop_kg_backup_tables_down.sql (no-op — the snapshots are
-- not restorable and are not needed).

DROP TABLE IF EXISTS graph_nodes_backup_066;
DROP TABLE IF EXISTS graph_nodes_backup_069;
DROP TABLE IF EXISTS graph_nodes_backup_073;
DROP TABLE IF EXISTS graph_nodes_backup_074;
DROP TABLE IF EXISTS graph_nodes_backup_077;
DROP TABLE IF EXISTS graph_nodes_backup_091;
DROP TABLE IF EXISTS graph_nodes_backup_098;
DROP TABLE IF EXISTS graph_nodes_backup_125;

DROP TABLE IF EXISTS graph_edges_backup_066;
DROP TABLE IF EXISTS graph_edges_backup_073;
DROP TABLE IF EXISTS graph_edges_backup_074;
DROP TABLE IF EXISTS graph_edges_backup_077;
DROP TABLE IF EXISTS graph_edges_backup_085;
DROP TABLE IF EXISTS graph_edges_backup_091;
DROP TABLE IF EXISTS graph_edges_backup_098;
DROP TABLE IF EXISTS graph_edges_backup_125;
DROP TABLE IF EXISTS graph_edges_backup_132;
