-- Migration 097: ingest_change_history audit table
-- v1.0.0 - 2026-04-29 - Phase 1.5 P1.5.E4 ingest_change_history audit table
--
-- Purpose: track changes (project_slug / target_folder / target_filename) on
-- pending ingest rows after upload. Enables:
--   - audit trail (who moved which file when)
--   - undo/diagnostics for accidental project changes
--   - regression analysis on misclassified uploads
--
-- Prerequisite: migration 094 (ingest_pending). FK with ON DELETE CASCADE so
-- history rows disappear with the parent ingest_pending row.
-- Rollback: migrations/097_ingest_change_history_down.sql
-- Apply: sqlite3 /data/pir/console.db < migrations/097_ingest_change_history.sql

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ingest_change_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_pending_id TEXT NOT NULL,
    field_name TEXT NOT NULL CHECK(field_name IN (
        'project_slug', 'target_folder', 'target_filename'
    )),
    old_value TEXT,
    new_value TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    source_ip TEXT,
    user_agent TEXT,
    FOREIGN KEY (ingest_pending_id) REFERENCES ingest_pending(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_change_hist_ingest_id
    ON ingest_change_history(ingest_pending_id, changed_at DESC);

INSERT OR IGNORE INTO schema_versions(version) VALUES (97);

COMMIT;
