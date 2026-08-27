BEGIN IMMEDIATE;

ALTER TABLE project_teams DROP COLUMN role;
ALTER TABLE project_teams DROP COLUMN clearance;
DELETE FROM schema_versions WHERE version = 160;

COMMIT;
