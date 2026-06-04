-- Down migration for 128_brain_drift_signals.sql
-- Removes the drift signals table and all related indexes/triggers.
-- DROP-only: no ALTER on sub-01 substrate or sibling Brain tables.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_brain_drift_signals_updated_at;

DROP INDEX IF EXISTS idx_brain_drift_signals_scope_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_scope_type;
DROP INDEX IF EXISTS idx_brain_drift_signals_type_cycle;
DROP INDEX IF EXISTS idx_brain_drift_signals_observed_ref;
DROP INDEX IF EXISTS idx_brain_drift_signals_run;
DROP INDEX IF EXISTS idx_brain_drift_signals_recurrence;
DROP INDEX IF EXISTS idx_brain_drift_signals_open_severity;
DROP INDEX IF EXISTS idx_brain_drift_signals_lookback;
DROP INDEX IF EXISTS idx_brain_drift_signals_axis;

DROP TABLE IF EXISTS brain_drift_signals;

DELETE FROM schema_versions WHERE version = 128;

COMMIT;
