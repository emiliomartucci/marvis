-- Migration 065 — Knowledge Graph Spike (v2.1 post-deepen)
-- Reversibile: DROP TABLE graph_edges; DROP TABLE graph_nodes; DELETE FROM schema_versions WHERE version=65;
-- Pre-req: PRAGMA foreign_keys=ON in connection factory (verified in api/db.py)

CREATE TABLE IF NOT EXISTS graph_nodes (
    id TEXT PRIMARY KEY,                    -- "function:api.db.get_write_db" (lowercase, stripped)
    type TEXT NOT NULL CHECK(type IN ('function','file','module')),
    name TEXT NOT NULL,                     -- "get_write_db"
    qualified_name TEXT NOT NULL,           -- "api.db.get_write_db"
    file_path TEXT,
    line_number INTEGER,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_graph_nodes_qualified_name ON graph_nodes(qualified_name);
CREATE INDEX IF NOT EXISTS idx_graph_nodes_type ON graph_nodes(type);

CREATE TABLE IF NOT EXISTS graph_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    relation TEXT NOT NULL CHECK(relation IN ('calls','imports','defines')),
    confidence REAL NOT NULL DEFAULT 1.0 CHECK(confidence BETWEEN 0.0 AND 1.0),
    source TEXT NOT NULL DEFAULT 'ast' CHECK(source IN ('ast','rem','llm','manual')),
    metadata TEXT NOT NULL DEFAULT '{}',
    source_file TEXT,
    source_line INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (source_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES graph_nodes(id) ON DELETE CASCADE,
    UNIQUE(source_id, target_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_graph_edges_source ON graph_edges(source_id, relation);
CREATE INDEX IF NOT EXISTS idx_graph_edges_target ON graph_edges(target_id, relation);

-- Schema version bump (MarvisX pattern: single-value insert, default timestamp)
INSERT OR IGNORE INTO schema_versions (version) VALUES (65);
