-- 022_devx_agent_managed_down.sql
-- Rollback: SQLite < 3.35 non supporta DROP COLUMN
-- Per rollback completo: ricrea tabella senza la colonna
DROP INDEX IF EXISTS idx_sessions_agent_managed;
-- Note: ALTER TABLE ... DROP COLUMN richiede SQLite >= 3.35
-- SELECT sqlite_version() per verificare
