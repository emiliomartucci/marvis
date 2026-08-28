-- Brain agent-native (decision 2026-07-01-brain-agent-native): the user's own
-- agent writes a finding (its own conclusion) via brain_write_finding.
-- authored_by_agent records the writing agent's identity; NULL means the finding
-- came from the mechanical cycle rules (F1-F6). Provenance is kept SEPARATE so
-- agent conclusions are distinguishable and queryable, mirroring narrative_agent
-- on brain_journal_entries (migration 158).

ALTER TABLE brain_findings ADD COLUMN authored_by_agent TEXT;
