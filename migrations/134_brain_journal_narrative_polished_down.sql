-- Down migration: drop narrative_polished columns. SQLite ALTER TABLE DROP
-- COLUMN requires 3.35+; the runtime is 3.45+ so we use it directly.

ALTER TABLE brain_journal_entries DROP COLUMN narrative_polished_model;
ALTER TABLE brain_journal_entries DROP COLUMN narrative_polished_at;
ALTER TABLE brain_journal_entries DROP COLUMN narrative_polished;
