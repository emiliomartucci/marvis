-- 027_teams_auth_phase_b.sql
-- SOLO additive -- nessun DROP/ALTER su tabelle esistenti

CREATE TABLE IF NOT EXISTS teams (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  display_name TEXT NOT NULL,
  description TEXT,
  parent_team_id TEXT REFERENCES teams(id),
  created_by TEXT REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  deleted_at TEXT
);

CREATE TABLE IF NOT EXISTS team_members (
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  is_admin INTEGER NOT NULL DEFAULT 0,
  joined_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  PRIMARY KEY (team_id, user_id)
);

CREATE TABLE IF NOT EXISTS project_teams (
  project TEXT NOT NULL,
  team_id TEXT NOT NULL REFERENCES teams(id) ON DELETE CASCADE,
  is_public INTEGER NOT NULL DEFAULT 0,
  assigned_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
  PRIMARY KEY (project, team_id)
);

CREATE INDEX IF NOT EXISTS idx_team_members_user ON team_members(user_id);
CREATE INDEX IF NOT EXISTS idx_project_teams_team ON project_teams(team_id);
CREATE INDEX IF NOT EXISTS idx_project_teams_project ON project_teams(project);

INSERT OR IGNORE INTO schema_versions (version) VALUES (27);
