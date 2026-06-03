-- Down migration for 130_brain_findings.sql
-- Removes Learn finding tables, indexes, triggers.
-- DROP-only: no ALTER on sub-01/02/03 sibling Brain tables.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_brain_findings_terminal_forward_only;
DROP TRIGGER IF EXISTS trg_brain_findings_no_delete_resolved;
DROP TRIGGER IF EXISTS trg_brain_findings_updated_at;

DROP INDEX IF EXISTS idx_brain_findings_evidence_finding;
DROP INDEX IF EXISTS idx_brain_findings_evidence_lookup;
DROP INDEX IF EXISTS idx_brain_findings_states_actor;
DROP INDEX IF EXISTS idx_brain_findings_states_finding;
DROP INDEX IF EXISTS idx_brain_findings_regression;
DROP INDEX IF EXISTS idx_brain_findings_run;
DROP INDEX IF EXISTS idx_brain_findings_type_cycle;
DROP INDEX IF EXISTS idx_brain_findings_fingerprint;
DROP INDEX IF EXISTS idx_brain_findings_open;
DROP INDEX IF EXISTS idx_brain_findings_scope_cycle;
DROP INDEX IF EXISTS uk_brain_findings_natural;

DROP TABLE IF EXISTS brain_finding_evidence;
DROP TABLE IF EXISTS brain_finding_states;
DROP TABLE IF EXISTS brain_findings;

DELETE FROM schema_versions WHERE version = 130;

COMMIT;
