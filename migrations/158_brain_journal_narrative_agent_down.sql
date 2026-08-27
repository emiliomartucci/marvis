-- Down migration: drop agent-narrative columns. SQLite ALTER TABLE DROP COLUMN
-- requires 3.35+; run manually (the migration runner ignores *_down.sql).
ALTER TABLE brain_journal_entries DROP COLUMN narrative_agent_by;
ALTER TABLE brain_journal_entries DROP COLUMN narrative_agent_at;
ALTER TABLE brain_journal_entries DROP COLUMN narrative_agent;
