-- Down migration 122 — DESTRUCTIVE ROLLBACK
-- WARNING: questa migration cancella TUTTI i drift history records.
-- Backup raccomandato prima del rollback:
--   sqlite3 /data/pir/console.db \
--     "CREATE TABLE docs_drift_history_backup AS SELECT * FROM docs_drift_history;"
-- Eseguire down SOLO dopo conferma esplicita umana.

BEGIN IMMEDIATE;

DROP INDEX IF EXISTS idx_drift_opened_task;
DROP INDEX IF EXISTS idx_drift_fingerprint_open;
DROP TABLE IF EXISTS docs_drift_history;
DELETE FROM schema_versions WHERE version = 122;

COMMIT;
