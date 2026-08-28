DROP INDEX IF EXISTS idx_gui_events_workspace_seen;
DROP TABLE IF EXISTS gui_events;
DELETE FROM schema_versions WHERE version = 163;
