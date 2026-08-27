-- Migration 173 — brain_findings: reject a malformed finding_id at write time
--
-- Follow-up to PR #253 (findings read-path resilience). brain_findings.finding_id
-- is `TEXT PRIMARY KEY` with NO type/length CHECK — unlike proposal_fingerprint
-- (length = 32) and evidence_hash (length = 64), which the DB already enforces. So
-- a NULL / BLOB / wrong-length id can be persisted and later fail the read model
-- (core.api.models.brain.Finding.finding_id requires exactly 32 chars). #253 made
-- the read path skip such a poison row; this migration closes the door upstream so
-- no NEW one can land.
--
-- Form: two BEFORE-write triggers, NOT a CHECK constraint. Adding a *restricting*
-- CHECK to brain_findings would require the full table rebuild (the table carries
-- 12+ columns, 3 self-FKs, 6 indexes and 3 triggers, plus later ALTERs in
-- 133/159/165) and — critically — the row-copy step would ABORT the boot migration
-- if the pre-existing corrupt row were still present. Migrations run on every boot,
-- and a failed one blocks startup (learning 8f8f5c97). A trigger validates only NEW
-- writes, never touches existing rows, and delivers the same "no new malformed
-- finding_id" guarantee with zero deploy risk. It mirrors the table's existing
-- invariant triggers (trg_brain_findings_terminal_forward_only,
-- trg_brain_findings_no_delete_resolved).
--
-- Predicate (matches Finding.finding_id = 32-char str): a text value of length 32.
-- typeof(...) <> 'text' also rejects NULL (typeof 'null'), BLOB ('blob') and
-- numeric ids, so length() is only evaluated on an actual text value.
--
-- The pre-existing corrupt row (if any) is intentionally NOT cleaned here: the read
-- path already skips + counts it, and removing it is a separate, deliberate operator
-- action once its id surfaces in the WARNING log.
--
-- Reversible: migrations/173_brain_findings_finding_id_shape_guard_down.sql.

BEGIN IMMEDIATE;

CREATE TRIGGER IF NOT EXISTS trg_brain_findings_finding_id_shape_insert
    BEFORE INSERT ON brain_findings
    FOR EACH ROW
    WHEN typeof(NEW.finding_id) <> 'text' OR length(NEW.finding_id) <> 32
BEGIN
    SELECT RAISE(ABORT, 'finding_id must be a 32-character text id');
END;

CREATE TRIGGER IF NOT EXISTS trg_brain_findings_finding_id_shape_update
    BEFORE UPDATE OF finding_id ON brain_findings
    FOR EACH ROW
    WHEN typeof(NEW.finding_id) <> 'text' OR length(NEW.finding_id) <> 32
BEGIN
    SELECT RAISE(ABORT, 'finding_id must be a 32-character text id');
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (173);

COMMIT;
