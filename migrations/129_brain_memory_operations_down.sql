-- Down migration for 129_brain_memory_operations.sql
-- Removes memory operation tables, indexes, triggers.
-- DROP-only: no ALTER on sub-01/sub-02 sibling Brain tables.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_brain_mo_no_delete_applied;
DROP TRIGGER IF EXISTS trg_brain_mo_updated_at;

DROP INDEX IF EXISTS idx_brain_mo_evidence_op;
DROP INDEX IF EXISTS idx_brain_mo_evidence_lookup;
DROP INDEX IF EXISTS idx_brain_mo_states_actor;
DROP INDEX IF EXISTS idx_brain_mo_states_op;
DROP INDEX IF EXISTS idx_brain_mo_run;
DROP INDEX IF EXISTS idx_brain_mo_type_cycle;
DROP INDEX IF EXISTS idx_brain_mo_recurrence;
DROP INDEX IF EXISTS idx_brain_mo_pending;
DROP INDEX IF EXISTS idx_brain_mo_scope_cycle;
DROP INDEX IF EXISTS uk_brain_mo_natural;

DROP TABLE IF EXISTS brain_memory_operation_evidence;
DROP TABLE IF EXISTS brain_memory_operation_states;
DROP TABLE IF EXISTS brain_memory_operations;

DELETE FROM schema_versions WHERE version = 129;

COMMIT;
