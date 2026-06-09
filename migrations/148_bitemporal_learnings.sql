-- Migration 148 — Track 2 #1-S1: bitemporal columns on learnings.
--
-- ADDITIVE: new columns default NULL + indexes + backfill — NO behavior change
-- (no read/write path consumes these yet; that is S2/S3). VERIFY against prod
-- schema_versions max before merge: this file is numbered 148 from the local
-- `ls migrations/` (last applied = 147). If prod has advanced past 147 by the
-- time this lands, renumber to (prod max + 1) — the orchestrator will confirm.
--
-- Bi-temporal model (Zep/Graphiti arXiv:2501.13956), minimum viable = the
-- SYSTEM-TIME axis only: valid_from = when the system learned the fact,
-- invalid_at = when the system retracted it (NULL = live). Two columns are the
-- floor for a real `as_of` audit ("what we BELIEVED on 2026-05-29"): a single
-- invalid_at collapses the timelines and cannot answer it. The full Graphiti
-- 4-column variant (separate real-world validity range distinct from ingest) is
-- deferred — not needed until decisions carry valid-time distinct from when we
-- ingested them. KG edges already ship this pattern (`valid_until` + as_of), so
-- this mirrors a working in-codebase precedent rather than inventing one.
--
-- superseded_by / supersede_reason record the audit chain: on a contradicting
-- write (S3) the old row gets invalid_at + superseded_by = id of the new live
-- row + a human-readable reason. The row is never deleted (reversible, audited).
--
-- No `decisions` (or equivalent memory-of-decisions) table exists yet — the only
-- *_decisions tables are inbox_triage_decisions / docs_triage_decisions, which are
-- governance/triage logs, not a learnings-style knowledge store. When a decisions
-- store lands, mirror these four columns + the two indexes onto it.
--
-- Reversibile: see 148_bitemporal_learnings_down.sql

ALTER TABLE learnings ADD COLUMN valid_from TIMESTAMP;       -- system-time: when learned (NULL until backfilled below)
ALTER TABLE learnings ADD COLUMN invalid_at TIMESTAMP;       -- system-time: when retracted; NULL = live
ALTER TABLE learnings ADD COLUMN superseded_by TEXT;         -- id of the replacement learning row (audit chain)
ALTER TABLE learnings ADD COLUMN supersede_reason TEXT;      -- why it was superseded (human-readable)

-- Backfill system-time for existing rows. Idempotent (WHERE valid_from IS NULL),
-- so re-running the migration is a no-op. On a huge table this should be chunked
-- inside the writer txn; the learnings table is small (hundreds of rows), so a
-- single statement is fine here.
UPDATE learnings SET valid_from = COALESCE(created_at, CURRENT_TIMESTAMP) WHERE valid_from IS NULL;

-- Partial index = the load-bearing hot path: default reads filter live rows only.
CREATE INDEX IF NOT EXISTS idx_learnings_live ON learnings(invalid_at) WHERE invalid_at IS NULL;

-- Range index for `as_of` point-in-time scans (valid_from <= T AND (invalid_at IS NULL OR invalid_at > T)).
CREATE INDEX IF NOT EXISTS idx_learnings_asof ON learnings(valid_from, invalid_at);

INSERT OR IGNORE INTO schema_versions (version) VALUES (148);
