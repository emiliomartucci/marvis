BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_project_gui_metadata_project;
DROP TABLE IF EXISTS project_gui_metadata;
DELETE FROM schema_versions WHERE version = 152;

COMMIT;
