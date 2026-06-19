-- Migration 125 — KG: add `record` graph node type
--
-- `record` is the generic bucket for factual/admin documents ingested by
-- MarvisX: invoices, bills, receipts, bank statements, company registry
-- records, identity documents, certificates, and official notices.
--
-- SQLite cannot alter a CHECK constraint in place, so this mirrors the
-- graph_nodes rebuild pattern used by migrations 091 and 098. Unlike 098, this
-- migration preserves graph_edges.weight / last_touched_at (migration 099) and
-- graph_nodes.kg_enriched_at (migration 100).

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

DROP TABLE IF EXISTS graph_nodes_backup_125;
DROP TABLE IF EXISTS graph_edges_backup_125;
CREATE TABLE graph_nodes_backup_125 AS SELECT * FROM graph_nodes;
CREATE TABLE graph_edges_backup_125 AS SELECT * FROM graph_edges;

DROP TRIGGER IF EXISTS graph_nodes_fts_insert;
DROP TRIGGER IF EXISTS graph_nodes_fts_update;
DROP TRIGGER IF EXISTS graph_nodes_fts_delete;
DROP TRIGGER IF EXISTS trg_kg_pins_cleanup;

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

CREATE TABLE graph_nodes_new (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL CHECK(type IN (
        'function','file','module',
        'task','pr','commit',
        'handoff','solution','learning',
        'audit','spike','analysis','research','rubric','guide','mockup',
        'project',
        'hook','skill','command','plugin',
        'plan','brainstorm',
        'inbox',
        'xlsx',
        'policy','contract','transcript','record','report'
    )),
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    file_path TEXT,
    line_number INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    first_seen_git_sha TEXT,
    last_modified_git_sha TEXT,
    last_seen_at TEXT,
    deprecated_at TEXT,
    touch_count_total INTEGER NOT NULL DEFAULT 0,
    touch_count_7d INTEGER NOT NULL DEFAULT 0,
    touch_count_30d INTEGER NOT NULL DEFAULT 0,
    touch_authors TEXT NOT NULL DEFAULT '[]',
    touch_last_at TEXT,
    project_id TEXT,
    degree INTEGER NOT NULL DEFAULT 0,
    kg_enriched_at TEXT
);

INSERT INTO graph_nodes_new (
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at, project_id, degree, kg_enriched_at
)
SELECT
    id, type, name, qualified_name, file_path, line_number, metadata,
    created_at, updated_at,
    first_seen_git_sha, last_modified_git_sha, last_seen_at, deprecated_at,
    touch_count_total, touch_count_7d, touch_count_30d, touch_authors,
    touch_last_at, project_id, degree, kg_enriched_at
FROM graph_nodes;

DROP TABLE graph_nodes;
ALTER TABLE graph_nodes_new RENAME TO graph_nodes;

DELETE FROM sqlite_sequence WHERE name='graph_edges';
INSERT INTO sqlite_sequence (name, seq)
SELECT 'graph_edges', COALESCE(MAX(id), 0) FROM graph_edges;
UPDATE sqlite_sequence
   SET seq = (SELECT COALESCE(MAX(id), 0) FROM graph_edges)
 WHERE name = 'graph_edges';

CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);
CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_last_seen ON graph_nodes(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_first_seen ON graph_nodes(created_at);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_deprecated ON graph_nodes(deprecated_at)
    WHERE deprecated_at IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_edges_first_seen ON graph_edges(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_graph_edges_validity ON graph_edges(valid_until)
    WHERE valid_until IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d
    ON graph_nodes(touch_count_30d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_7d
    ON graph_nodes(touch_count_7d DESC)
    WHERE type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project ON graph_nodes(project_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_project_relation
    ON graph_edges(project_id, relation, source_id, target_id);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target_relation
    ON graph_edges(target_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_touch_30d_active
    ON graph_nodes(touch_count_30d DESC)
    WHERE deprecated_at IS NULL AND type IN ('function','file');
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_type_active
    ON graph_nodes(project_id, type)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_project_lastseen_active
    ON graph_nodes(project_id, last_seen_at DESC)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_degree_type
    ON graph_nodes(type, degree DESC)
    WHERE deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_metadata_path
    ON graph_nodes(json_extract(metadata, '$.path'))
    WHERE type = 'file' AND deprecated_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_graph_nodes_enriched_pending
    ON graph_nodes(kg_enriched_at)
    WHERE kg_enriched_at IS NULL AND deprecated_at IS NULL;

CREATE TRIGGER graph_nodes_fts_insert AFTER INSERT ON graph_nodes
WHEN NEW.deprecated_at IS NULL
BEGIN
    INSERT INTO graph_nodes_fts(id, name, qualified_name, file_path, metadata_text, type, project_id)
    VALUES (
        NEW.id,
        NEW.name,
        NEW.qualified_name,
        COALESCE(NEW.file_path, ''),
        COALESCE(NEW.metadata, '{}'),
        NEW.type,
        COALESCE(NEW.project_id, '')
    );
END;

CREATE TRIGGER graph_nodes_fts_update AFTER UPDATE ON graph_nodes
BEGIN
    DELETE FROM graph_nodes_fts WHERE id = OLD.id;
    INSERT INTO graph_nodes_fts(id, name, qualified_name, file_path, metadata_text, type, project_id)
    SELECT
        NEW.id,
        NEW.name,
        NEW.qualified_name,
        COALESCE(NEW.file_path, ''),
        COALESCE(NEW.metadata, '{}'),
        NEW.type,
        COALESCE(NEW.project_id, '')
    WHERE NEW.deprecated_at IS NULL;
END;

CREATE TRIGGER graph_nodes_fts_delete AFTER DELETE ON graph_nodes
BEGIN
    DELETE FROM graph_nodes_fts WHERE id = OLD.id;
END;

CREATE TRIGGER trg_kg_pins_cleanup
AFTER UPDATE OF deprecated_at ON graph_nodes
WHEN NEW.deprecated_at IS NOT NULL
BEGIN
    DELETE FROM kg_pins WHERE node_id = NEW.id;
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (125);

COMMIT;

PRAGMA foreign_keys=ON;
