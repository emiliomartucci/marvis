-- v1.0.0 - 2026-02-26 - Task scoring columns for triage dashboard (ICE-D framework)
ALTER TABLE tasks ADD COLUMN impact INTEGER;
ALTER TABLE tasks ADD COLUMN confidence INTEGER;
ALTER TABLE tasks ADD COLUMN ease INTEGER;
ALTER TABLE tasks ADD COLUMN delegation TEXT;
-- VIRTUAL generated column: computed on read, not stored. Requires SQLite 3.31+
ALTER TABLE tasks ADD COLUMN ice_score INTEGER GENERATED ALWAYS AS (impact * confidence * ease) VIRTUAL;
-- Scoring attribution (future-proofing)
ALTER TABLE tasks ADD COLUMN scored_by TEXT;
ALTER TABLE tasks ADD COLUMN scored_at TEXT;

CREATE INDEX IF NOT EXISTS idx_tasks_ice_score ON tasks(ice_score);
