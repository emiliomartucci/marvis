-- Migration 095: intent-first schema for Universal Ingestion phase 1 and
-- future KG myelination/decay work.
-- v1.0.0 - 2026-04-27
--
-- Prerequisite: migration 094 applied.
--
-- Production apply procedure:
--   1. systemctl --user stop pir-api.service
--   2. systemctl --user stop pir-kg-watcher.service
--   3. sqlite3 /data/pir/console.db "PRAGMA wal_checkpoint(TRUNCATE);"
--   4. cp /data/pir/console.db /data/pir/backups/console.db.pre-095-$(date +%Y%m%d-%H%M%S).bak
--   5. time sqlite3 /data/pir/console.db < <repo>/migrations/095_kg_intent_first.sql
--   6. systemctl --user start pir-kg-watcher.service
--   7. systemctl --user start pir-api.service
--   8. curl -fsS http://localhost:8100/healthz
--
-- Dry-run requirement before production:
--   cp /data/pir/console.db /tmp/console-dryrun.db
--   time sqlite3 /tmp/console-dryrun.db < <repo>/migrations/095_kg_intent_first.sql
--
-- Abort if dry-run or production apply takes >= 15s. Expected duration on the
-- 2026-04-27 production graph_edges table (~81k rows): below 10s.
--
-- Project opt-in note:
--   The plan text mentioned projects.allow_external_embed, but the live MarvisX
--   DB has no projects table; project metadata is filesystem-backed. This
--   migration therefore creates project_external_embedding_policy keyed by
--   project_slug, preserving the opt-in requirement without inventing a
--   duplicate projects table.

BEGIN IMMEDIATE;

-- Future KG myelination hooks. Phase 1 only writes the schema.
ALTER TABLE graph_edges ADD COLUMN weight REAL NOT NULL DEFAULT 1.0;
ALTER TABLE graph_edges ADD COLUMN last_touched_at TEXT;
UPDATE graph_edges
   SET last_touched_at = COALESCE(first_seen_at, datetime('now'))
 WHERE last_touched_at IS NULL;

-- External (remote) embedding opt-in policy. Missing row means false in code.
CREATE TABLE project_external_embedding_policy (
    project_slug TEXT PRIMARY KEY,
    allow_external_embed INTEGER NOT NULL DEFAULT 0 CHECK(allow_external_embed IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO project_external_embedding_policy(project_slug, allow_external_embed)
SELECT DISTINCT project_id, 0
  FROM graph_nodes
 WHERE project_id IS NOT NULL
   AND project_id != '';

-- Edge activity ledger. Initial phase 1 use: ingest_insert events.
CREATE TABLE kg_edge_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edge_source_id TEXT NOT NULL,
    edge_target_id TEXT NOT NULL,
    edge_relation TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'ingest_insert', 'lookup', 'citation', 'impact', 'manual_pin'
    )),
    source_agent TEXT,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (edge_source_id, edge_target_id, edge_relation)
        REFERENCES graph_edges(source_id, target_id, relation) ON DELETE CASCADE
);

CREATE INDEX idx_kg_edge_activity_event
    ON kg_edge_activity(event_type, created_at DESC);
CREATE INDEX idx_kg_edge_activity_edge
    ON kg_edge_activity(edge_source_id, edge_target_id, edge_relation);

INSERT OR IGNORE INTO schema_versions(version) VALUES (95);

COMMIT;
