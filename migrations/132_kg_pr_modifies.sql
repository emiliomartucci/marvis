-- Migration 132 — KG PR-Impact substrate (sub-01 D1)
--
-- Adds the `modifies` edge type to graph_edges + 3 sidecar tables
-- (pr_function_touches, webhook_deliveries, pr_impact_jobs) used by the
-- KG PR-Impact View pipeline (parent plan
-- docs/plans/2026-05-16-feat-kg-pr-impact-view-plan.md, sub-01 §4.1).
--
-- SQLite cannot ALTER a CHECK constraint in place, so graph_edges is
-- rebuilt using the same pattern as migrations 091 / 098 / 125. The
-- migration is offline-only: pir-api MUST be stopped before applying
-- (single-writer invariant, see learning 6130bc49 + 4d4278e4).
--
-- Preserved: all 16 graph_edges columns, FK CASCADE to graph_nodes,
-- UNIQUE(source_id, target_id, relation), 6 explicit indexes.
--
-- New edge type semantics (referenced by sub-01 D2 populator):
--   `modifies` : pr_artifact -> function_artifact, weighted by
--                touched_lines / total_lines, source='git', metadata
--                carries blame_author + blame_sha + diff_lines.
--
-- Rollback: migrations/132_kg_pr_modifies_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/132_kg_pr_modifies.sql

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- Section A : rebuild graph_edges with `modifies` in CHECK relation
-- ------------------------------------------------------------------

DROP TABLE IF EXISTS graph_edges_backup_132;
CREATE TABLE graph_edges_backup_132 AS SELECT * FROM graph_edges;

CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',
        'produces','contains',
        'describes','documents','cites','applies_to',
        'depends_on','mentions','refers_to','shares_tag','similar_to',
        'resolves_to',
        'modifies'
    )),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    source TEXT NOT NULL DEFAULT 'ast' CHECK(source IN (
        'ast','git','db','frontmatter','rem','llm','manual'
    )),
    metadata TEXT NOT NULL DEFAULT '{}',
    source_file TEXT,
    source_line INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    first_seen_at TEXT,
    last_seen_at TEXT,
    valid_until TEXT,
    project_id TEXT,
    weight REAL NOT NULL DEFAULT 1.0,
    last_touched_at TEXT,
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);

INSERT INTO graph_edges_new (
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until, project_id,
    weight, last_touched_at
)
SELECT
    id, source_id, target_id, relation, confidence, source,
    metadata, source_file, source_line, created_at,
    first_seen_at, last_seen_at, valid_until, project_id,
    weight, last_touched_at
FROM graph_edges;

DROP TABLE graph_edges;
ALTER TABLE graph_edges_new RENAME TO graph_edges;

-- Preserve AUTOINCREMENT high-water-mark so future inserts pick a
-- monotonically increasing id (mig 125 pattern).
DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

-- Recreate the 6 explicit indexes (UNIQUE auto-index comes free).
CREATE INDEX IF NOT EXISTS idx_graph_edges_source
    ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target
    ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen
    ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity
    ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);

-- ------------------------------------------------------------------
-- Section B : pr_function_touches (PR -> function blame audit)
-- ------------------------------------------------------------------
-- Populated by scripts/populate_pr_impact.py (sub-01 D2). One row per
-- (pr, function, hunk-range). `blame_author` is PII tracked in
-- docs/privacy/pii-inventory.md §5; populator is the only writer.
--
-- function_id ON DELETE SET NULL preserves the audit trail even after
-- the function node is purged (deprecated_at sweep). qualified_name
-- snapshot keeps the row readable post-delete.

CREATE TABLE IF NOT EXISTS pr_function_touches (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    pr_id                    TEXT NOT NULL
                             REFERENCES pull_requests(id) ON DELETE CASCADE,
    function_id              TEXT
                             REFERENCES graph_nodes(id) ON DELETE SET NULL,
    qualified_name_snapshot  TEXT NOT NULL,
    source_file              TEXT NOT NULL,
    source_line_start        INTEGER NOT NULL,
    source_line_end          INTEGER NOT NULL,
    touched_lines            INTEGER NOT NULL DEFAULT 0,
    total_lines              INTEGER NOT NULL DEFAULT 0,
    weight                   REAL NOT NULL DEFAULT 1.0
                             CHECK(weight BETWEEN 0.0 AND 1.0),
    blame_author             TEXT,
    blame_commit_sha         TEXT,
    diff_added               INTEGER NOT NULL DEFAULT 0,
    diff_removed             INTEGER NOT NULL DEFAULT 0,
    project_id               TEXT,
    populator_version        TEXT NOT NULL DEFAULT 'v1',
    created_at               TEXT NOT NULL
                             DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(pr_id, qualified_name_snapshot, source_line_start, source_line_end)
);

CREATE INDEX IF NOT EXISTS idx_pr_function_touches_pr
    ON pr_function_touches(pr_id);
CREATE INDEX IF NOT EXISTS idx_pr_function_touches_function
    ON pr_function_touches(function_id)
    WHERE function_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pr_function_touches_blame_author
    ON pr_function_touches(blame_author)
    WHERE blame_author IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pr_function_touches_project
    ON pr_function_touches(project_id);
CREATE INDEX IF NOT EXISTS idx_pr_function_touches_created
    ON pr_function_touches(created_at DESC);

-- ------------------------------------------------------------------
-- Section C : webhook_deliveries (HMAC + idempotency log)
-- ------------------------------------------------------------------
-- Append-only log of inbound webhook deliveries. Idempotency key is
-- `delivery_id` (X-GitHub-Delivery / X-Hub-Delivery / synthetic). The
-- handler (sub-01 D3) MUST INSERT OR IGNORE before processing; a
-- duplicate delivery falls through silently. `payload_sha256` of the
-- raw request body lets us detect mismatched replays vs genuine retry.

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id      TEXT PRIMARY KEY,
    source           TEXT NOT NULL
                     CHECK(source IN ('github','gitea','manual','synthetic')),
    event_type       TEXT NOT NULL,
    pr_id            TEXT
                     REFERENCES pull_requests(id) ON DELETE SET NULL,
    payload_sha256   TEXT NOT NULL CHECK(length(payload_sha256) = 64),
    received_at      TEXT NOT NULL
                     DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    processed_at     TEXT,
    status           TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending','processed','failed','skipped','dead')),
    error_summary    TEXT,
    retry_count      INTEGER NOT NULL DEFAULT 0,
    project_id       TEXT
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_received
    ON webhook_deliveries(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status
    ON webhook_deliveries(status, received_at DESC)
    WHERE status IN ('pending','failed');
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_pr
    ON webhook_deliveries(pr_id)
    WHERE pr_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_source_event
    ON webhook_deliveries(source, event_type);

-- ------------------------------------------------------------------
-- Section D : pr_impact_jobs (BackgroundTasks queue, replaces ARQ)
-- ------------------------------------------------------------------
-- In-DB job ledger for the PR-impact populator. We deliberately
-- avoid Redis/ARQ for v1 (sub-01 v2 decision: zero new deps).
-- FastAPI BackgroundTasks dispatches; the worker UPDATEs status as it
-- progresses. Stuck `running` rows are reclaimable via cron sweep
-- after `claim_lease_until` expires.

CREATE TABLE IF NOT EXISTS pr_impact_jobs (
    job_id              TEXT PRIMARY KEY,
    delivery_id         TEXT
                        REFERENCES webhook_deliveries(delivery_id) ON DELETE SET NULL,
    pr_id               TEXT NOT NULL
                        REFERENCES pull_requests(id) ON DELETE CASCADE,
    status              TEXT NOT NULL DEFAULT 'queued'
                        CHECK(status IN ('queued','running','done','failed','dead')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    last_error          TEXT,
    payload_json        TEXT NOT NULL DEFAULT '{}'
                        CHECK(json_valid(payload_json)),
    enqueued_at         TEXT NOT NULL
                        DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at          TEXT,
    finished_at         TEXT,
    claim_lease_until   TEXT,
    project_id          TEXT
);

CREATE INDEX IF NOT EXISTS idx_pr_impact_jobs_status_enqueued
    ON pr_impact_jobs(status, enqueued_at)
    WHERE status IN ('queued','running');
CREATE INDEX IF NOT EXISTS idx_pr_impact_jobs_pr
    ON pr_impact_jobs(pr_id);
CREATE INDEX IF NOT EXISTS idx_pr_impact_jobs_lease
    ON pr_impact_jobs(claim_lease_until)
    WHERE status = 'running' AND claim_lease_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_pr_impact_jobs_failed
    ON pr_impact_jobs(status, finished_at DESC)
    WHERE status IN ('failed','dead');

-- ------------------------------------------------------------------
-- schema_versions register
-- ------------------------------------------------------------------
INSERT OR IGNORE INTO schema_versions (version) VALUES (132);

COMMIT;

PRAGMA foreign_keys=ON;
