-- 029_team_roles.sql
-- Flat teams: add role column to team_members, add avatar_color to teams
-- Migrate is_admin boolean to role text enum ('member'|'admin')

-- Add role column with default 'member'
ALTER TABLE team_members ADD COLUMN role TEXT NOT NULL DEFAULT 'member';

-- Migrate existing is_admin=1 rows to role='admin'
UPDATE team_members SET role = 'admin' WHERE is_admin = 1;

-- Add avatar_color to teams for UI display
ALTER TABLE teams ADD COLUMN avatar_color TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (29);
