-- Migration 068 — KG Fase 1e: Touch counter (code churn + hotspot detection)
--
-- Additive ALTER ADD COLUMN on graph_nodes (no table rebuild, preserves FKs and
-- indexes). Wrapped in BEGIN IMMEDIATE so a crash half-way doesn't leave us with
-- partial DDL (each ALTER auto-commits outside a transaction).
--
-- Populated by scripts/populate_touch_counter.py: one batched `git log` scan
-- aggregates per-file touch counts + distinct authors, then a single batched
-- UPDATE propagates the counters to every code node sharing that file_path.
--
-- File-level only in 1e (function-level deferred): propagating file touch to
-- every function defined in that file is a reasonable proxy for churn and keeps
-- the populator <30s even on the full MarvisX graph (~5800 nodes).
--
-- Reversible: DELETE FROM schema_versions WHERE version=68 + clear columns. The
-- columns are NULL/0 by default so leaving them on disk is zero-cost.
--
-- Dependencies: migration 067 (temporal columns). `deprecated_at` is consulted
-- by the hotspot endpoint — a deprecated node must not show up as a hotspot.

BEGIN IMMEDIATE;

-- ---- Touch counters (NOT NULL DEFAULT 0 so existing rows are instantly
-- queryable without a backfill) --------------------------------------------
ALTER TABLE graph_nodes ADD COLUMN touch_count_total INTEGER NOT NULL DEFAULT 0;
ALTER TABLE graph_nodes ADD COLUMN touch_count_7d INTEGER NOT NULL DEFAULT 0;
ALTER TABLE graph_nodes ADD COLUMN touch_count_30d INTEGER NOT NULL DEFAULT 0;

-- JSON array of distinct author emails — used for bus factor awareness on the
-- hotspot endpoint (len==1 is a strong signal of single-contributor risk).
ALTER TABLE graph_nodes ADD COLUMN touch_authors TEXT NOT NULL DEFAULT '[]';

-- ISO timestamp of the most recent commit that touched this file (used to sort
-- hotspots by recency if counts tie, and to cheaply answer "when was this last
-- modified"). NULL for nodes with no git history (test fixtures, artifacts).
ALTER TABLE graph_nodes ADD COLUMN touch_last_at TEXT;

-- ---- Indices (partial — hotspot queries only touch function/file) ---------
-- `WHERE type IN ('function','file')` keeps the index small: artifact nodes
-- (task/pr/commit/...) inherit touch_count=0 and are excluded by the endpoint
-- filter, so indexing them would waste bytes.
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d
  ON graph_nodes(touch_count_30d DESC)
  WHERE type IN ('function','file');

CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_7d
  ON graph_nodes(touch_count_7d DESC)
  WHERE type IN ('function','file');

INSERT OR IGNORE INTO schema_versions (version) VALUES (68);

COMMIT;
