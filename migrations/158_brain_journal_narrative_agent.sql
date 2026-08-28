-- Brain agent-native (decision 2026-07-01-brain-agent-native): the user's agent
-- writes the narrative synthesis via brain_write_journal. Provenance is kept
-- SEPARATE from the cycle's narrative_polished (migration 134) so the cycle and
-- the agent never overwrite each other. NULL until the agent writes;
-- deterministic body_json is always present as the never-null fallback.

ALTER TABLE brain_journal_entries ADD COLUMN narrative_agent TEXT;
ALTER TABLE brain_journal_entries ADD COLUMN narrative_agent_at TEXT;
ALTER TABLE brain_journal_entries ADD COLUMN narrative_agent_by TEXT;
