-- gh #20: migration 006 seeded a LEGACY project_dirs default
-- (["~/marvis/projects-work", "~/marvis/projects-personal"]) into every fresh
-- DB. At startup that row overrides the configured storage.projects_root, so
-- the project index scans directories that do not exist on any modern install
-- and /api/v1/projects returns [] on the local tier. Remove the row ONLY when
-- it still holds the untouched legacy default — values set by a user (Console
-- settings) or by a deploy are different and must win as before.
DELETE FROM settings
WHERE key = 'project_dirs'
  AND value = '["~/marvis/projects-work", "~/marvis/projects-personal"]';
