BEGIN IMMEDIATE;
DROP INDEX IF EXISTS idx_external_app_sync_runs_recent;
DROP TABLE IF EXISTS external_app_sync_runs;
DROP TABLE IF EXISTS external_app_accounts;
DROP TABLE IF EXISTS external_app_entitlements;
DELETE FROM schema_versions WHERE version = 191;
COMMIT;
