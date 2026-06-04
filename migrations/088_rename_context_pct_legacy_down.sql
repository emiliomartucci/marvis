-- Down migration for 088

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta RENAME COLUMN last_context_pct_legacy TO last_context_pct;

DELETE FROM schema_versions WHERE version = 88;
COMMIT;
