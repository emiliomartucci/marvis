-- Rollback migration 127: drop brain v1 substrate (sub-01 Digest + Journal).
-- DROP-only, no ALTER on existing substrate.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_brain_journal_entries_updated_at;
DROP TRIGGER IF EXISTS trg_brain_runs_updated_at;

DROP INDEX IF EXISTS uniq_brain_journal_entries_run_scope;
DROP INDEX IF EXISTS idx_brain_journal_entries_timeline;
DROP INDEX IF EXISTS idx_brain_journal_entries_cycle_scope;

DROP INDEX IF EXISTS uniq_brain_digest_events_nat;
DROP INDEX IF EXISTS idx_brain_digest_events_cycle_source_project;
DROP INDEX IF EXISTS idx_brain_digest_events_source_ref;
DROP INDEX IF EXISTS idx_brain_digest_events_run;
DROP INDEX IF EXISTS idx_brain_digest_events_type_cycle;
DROP INDEX IF EXISTS idx_brain_digest_events_cursor;

DROP INDEX IF EXISTS idx_brain_runs_cycle_status;
DROP INDEX IF EXISTS idx_brain_runs_running;
DROP INDEX IF EXISTS idx_brain_runs_triggered;
DROP INDEX IF EXISTS uniq_brain_runs_active_cycle;

DROP TABLE IF EXISTS brain_journal_entries;
DROP TABLE IF EXISTS brain_digest_events;
DROP TABLE IF EXISTS brain_source_watermarks;
DROP TABLE IF EXISTS brain_runs;

DELETE FROM schema_versions WHERE version = 127;

COMMIT;
