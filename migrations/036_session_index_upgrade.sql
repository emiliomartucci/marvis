-- v036: Performance indexes for session scoping and visibility queries
-- Migration 033 created idx_sessions_owner on sessions_meta(owner_id).
-- This upgrades to a composite covering index and adds visibility query indexes.

-- Upgrade sessions index: composite (owner_id, created_at DESC) for covering scan
DROP INDEX IF EXISTS idx_sessions_owner;

CREATE INDEX IF NOT EXISTS idx_sessions_meta_owner_created
    ON sessions_meta(owner_id, created_at DESC);
-- WHERE owner_id = ? ORDER BY created_at DESC → pure index scan, no filesort

-- Visibility query join columns — full table scan on every request without these

CREATE INDEX IF NOT EXISTS idx_team_members_user_team
    ON team_members(user_id, team_id);

CREATE INDEX IF NOT EXISTS idx_team_members_team_id
    ON team_members(team_id);

CREATE INDEX IF NOT EXISTS idx_teams_active
    ON teams(id) WHERE deleted_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_project_teams_team_project
    ON project_teams(team_id, project);

CREATE INDEX IF NOT EXISTS idx_project_teams_public
    ON project_teams(project) WHERE is_public = 1;

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (36, datetime('now', 'utc'));
