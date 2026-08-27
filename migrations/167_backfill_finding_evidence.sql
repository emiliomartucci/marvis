-- Migration 167 — backfill >=1 brain_finding_evidence row for evidence-less findings
--
-- emit_finding_dedup (DR8 direction_drift / DR9 task_probably_done) historically
-- INSERTed a finding writing only evidence_hash, never any brain_finding_evidence
-- rows. The strict Finding model requires evidence>=1 (models.brain.Finding), so
-- brain_findings_patch (and any read that validates against Finding) 500s on those
-- rows: an operator can't approve/dismiss/resolve them via MCP (chip task_10d42c98).
-- The forward code fix (findings.py emit_finding_dedup) is forward-only; this heals
-- the findings already created evidence-less.
--
-- Scope: only patchable states (open / pending_bootstrap). Terminal states
-- (approved/dismissed/resolved/applied/expired) are left untouched.
-- Idempotent: INSERT OR IGNORE + NOT EXISTS guard -> a 2nd run inserts 0 rows.
-- Per-tenant: runs on every tenant DB via the migration runner.

BEGIN IMMEDIATE;

INSERT OR IGNORE INTO brain_finding_evidence
    (finding_id, position, evidence_kind, evidence_ref, weight, cycle_key)
SELECT f.finding_id, 0, 'kg_node',
       COALESCE(NULLIF(f.entity_ref, ''), f.finding_id), 1.0, f.cycle_key
FROM brain_findings f
WHERE f.approval_state IN ('open', 'pending_bootstrap')
  AND NOT EXISTS (
      SELECT 1 FROM brain_finding_evidence e WHERE e.finding_id = f.finding_id
  );

COMMIT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (167);
