-- Migration 132 DOWN — rollback KG PR-Impact substrate
--
-- Removes pr_impact_jobs / webhook_deliveries / pr_function_touches,
-- then rebuilds graph_edges WITHOUT the `modifies` relation. Any
-- existing `modifies` rows are dropped first so the new CHECK passes.
-- This is destructive for the populator state — only the backup
-- table from the up-migration (graph_edges_backup_132) preserves the
-- pre-migration edge snapshot.
--
-- Apply: sqlite3 /data/pir/console.db < migrations/132_kg_pr_modifies_down.sql

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Drop dependent ledgers first (FK references to pull_requests).
DROP TABLE IF EXISTS pr_impact_jobs;
DROP TABLE IF EXISTS webhook_deliveries;
DROP TABLE IF EXISTS pr_function_touches;

-- Purge `modifies` edges before rebuilding the table with the older
-- CHECK constraint so the INSERT-SELECT cannot fail.
DELETE FROM graph_edges WHERE relation = 'modifies';

DROP TABLE IF EXISTS graph_edges_rollback_132;
CREATE TABLE graph_edges_rollback_132 AS SELECT * FROM graph_edges;

CREATE TABLE graph_edges_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN (
        'calls','imports','defines',
        'produces','contains',
        'describes','documents','cites','applies_to',
        'depends_on','mentions','refers_to','shares_tag','similar_to',
        'resolves_to'
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

DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

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

DELETE FROM schema_versions WHERE version = 132;

COMMIT;

PRAGMA foreign_keys=ON;
