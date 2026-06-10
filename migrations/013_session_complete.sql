-- 013_session_complete.sql
-- Add completed_at to session_costs to track when a session was explicitly completed.

ALTER TABLE session_costs ADD COLUMN completed_at TEXT;

CREATE INDEX IF NOT EXISTS idx_session_costs_completed
    ON session_costs(completed_at)
    WHERE completed_at IS NOT NULL;
