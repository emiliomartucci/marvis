BEGIN IMMEDIATE;
-- Fail before any destructive schema change if new conclusions collide under the old key.
-- Restore a verified pre-upgrade backup instead; never delete conclusions to downgrade.
CREATE UNIQUE INDEX uk_brain_findings_legacy_guard ON brain_findings
(cycle_key, finding_type, scope_type, scope_key, evidence_hash, closure_condition_kind);
DROP INDEX uk_brain_findings_natural;
CREATE UNIQUE INDEX uk_brain_findings_natural ON brain_findings
(cycle_key, finding_type, scope_type, scope_key, evidence_hash, closure_condition_kind);
DROP INDEX uk_brain_findings_legacy_guard;
DELETE FROM schema_versions WHERE version=188;
COMMIT;
