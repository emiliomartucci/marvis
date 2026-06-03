-- v1.0.0 - 2026-04-17 - KG pin persistence for /graph UX
-- node_id intentionally NOT FK'd to graph_nodes: soft-delete via deprecated_at
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS kg_pins (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  workspace_id TEXT NOT NULL DEFAULT 'ws_default',
  user_id      TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  node_id      TEXT NOT NULL,
  pinned_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
  note         TEXT CHECK(note IS NULL OR length(note) <= 500),
  UNIQUE(workspace_id, user_id, node_id)
);
CREATE INDEX IF NOT EXISTS idx_kg_pins_ws_user ON kg_pins(workspace_id, user_id, pinned_at DESC);
CREATE INDEX IF NOT EXISTS idx_kg_pins_node    ON kg_pins(node_id);
CREATE TRIGGER IF NOT EXISTS trg_kg_pins_cleanup
AFTER UPDATE OF deprecated_at ON graph_nodes
WHEN NEW.deprecated_at IS NOT NULL
BEGIN
  DELETE FROM kg_pins WHERE node_id = NEW.id;
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (82);
COMMIT;
PRAGMA foreign_keys=ON;
