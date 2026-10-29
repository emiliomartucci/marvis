BEGIN IMMEDIATE;
-- Preserve rows and references; content-distinct agent conclusions have distinct fingerprints.
DROP INDEX uk_brain_findings_natural;
CREATE UNIQUE INDEX uk_brain_findings_natural ON brain_findings
(cycle_key, finding_type, scope_type, scope_key, evidence_hash, closure_condition_kind, proposal_fingerprint);
INSERT OR IGNORE INTO schema_versions(version) VALUES (188);
COMMIT;
