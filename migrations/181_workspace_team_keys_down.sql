-- Migration 181 rollback. The copy into a globally-unique slug table fails
-- closed when two workspaces use the same slug; no tenant row is discarded.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS teams_workspace_required_insert;
DROP TRIGGER IF EXISTS teams_workspace_required_update;
DROP TRIGGER IF EXISTS teams_workspace_immutable;
DROP INDEX IF EXISTS idx_teams_workspace_slug;
DROP INDEX IF EXISTS idx_teams_workspace;
DROP INDEX IF EXISTS idx_teams_active;

CREATE TABLE teams_v181_down (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT,
  parent_team_id TEXT REFERENCES teams_v181_down(id),
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  deleted_at TEXT,
  avatar_color TEXT,
  workspace_id TEXT
);

INSERT INTO teams_v181_down
  (id,slug,display_name,description,parent_team_id,created_by,created_at,
   deleted_at,avatar_color,workspace_id)
SELECT id,slug,display_name,description,parent_team_id,created_by,created_at,
       deleted_at,avatar_color,workspace_id
FROM teams;

DROP TABLE teams;
ALTER TABLE teams_v181_down RENAME TO teams;
CREATE INDEX idx_teams_workspace ON teams(workspace_id);
CREATE INDEX idx_teams_active ON teams(id) WHERE deleted_at IS NULL;

DELETE FROM schema_versions WHERE version = 181;

COMMIT;
PRAGMA foreign_keys=ON;
