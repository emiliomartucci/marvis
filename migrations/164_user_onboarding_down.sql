-- Down for 164 (manual rollback only; the migration runner skips *_down.sql).
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;
DROP TABLE IF EXISTS user_profile;
DROP TABLE IF EXISTS user_onboarding;
DELETE FROM schema_versions WHERE version = 164;
COMMIT;
PRAGMA foreign_keys=ON;
