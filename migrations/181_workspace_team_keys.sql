-- Migration 181: team slugs are unique inside a workspace, not fleet-wide.
--
-- Rebuild is required because migration 027 declared slug UNIQUE inline and
-- SQLite cannot drop the generated auto-index. Child tables continue to point
-- at the canonical `teams` name; foreign keys are checked by migration tests
-- and re-enabled by the production runner immediately after executescript.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE teams_v181_new (
  id TEXT PRIMARY KEY,
  slug TEXT NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT,
  parent_team_id TEXT REFERENCES teams_v181_new(id),
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  deleted_at TEXT,
  avatar_color TEXT,
  workspace_id TEXT NOT NULL
);

INSERT INTO teams_v181_new
  (id,slug,display_name,description,parent_team_id,created_by,created_at,
   deleted_at,avatar_color,workspace_id)
SELECT id,slug,display_name,description,parent_team_id,created_by,created_at,
       deleted_at,avatar_color,COALESCE(NULLIF(trim(workspace_id),''),'ws_default')
FROM teams;

DROP TABLE teams;
ALTER TABLE teams_v181_new RENAME TO teams;

CREATE UNIQUE INDEX idx_teams_workspace_slug
ON teams(workspace_id, slug);
CREATE INDEX idx_teams_workspace ON teams(workspace_id);
CREATE INDEX idx_teams_active ON teams(id) WHERE deleted_at IS NULL;

CREATE TRIGGER teams_workspace_required_insert
BEFORE INSERT ON teams
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'team workspace_id required');
END;

CREATE TRIGGER teams_workspace_required_update
BEFORE UPDATE ON teams
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'team workspace_id required');
END;

CREATE TRIGGER teams_workspace_immutable
BEFORE UPDATE OF workspace_id ON teams
WHEN OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'team workspace_id immutable');
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (181);

COMMIT;
PRAGMA foreign_keys=ON;
