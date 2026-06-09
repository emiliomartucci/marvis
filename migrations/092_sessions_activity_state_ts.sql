-- Migration 092 — sessions_meta.activity_state_updated_at
--
-- Adds a server-side timestamp column tracking the last time the session's
-- activity_state was updated by an event-driven source (Claude Code hooks,
-- OpenCode plugin events). Used by api/routers/sessions.py:list_sessions to
-- gate the fallback path: if `now() - activity_state_updated_at < 60s`, trust
-- the column; otherwise, fall back to the legacy `tmux capture-pane` regex
-- detection (api/services/tmux.py:detect_activity_state).
--
-- This migration is intentionally minimal:
-- - ADD COLUMN only (no rebuild). sessions_meta is small (~20 rows).
-- - NO new index. The fallback gate is a per-row scan post-fetch (full table
--   ~5µs), and there is no query that filters by activity_state_updated_at.
--   Adding an index would be unused weight + WAL bloat (learning d2e1356f).
--
-- Reversibile: vedi migrations/092_sessions_activity_state_ts_down.sql.
-- Dipendenze: nessuna (sessions_meta esiste pre-001).
-- SQLite minimum: 3.25 (ADD COLUMN basic, no DROP COLUMN needed for down).
--
-- Plan: docs/plans/2026-04-26-feat-session-state-event-driven-plan.md (PR1)

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta
    ADD COLUMN activity_state_updated_at TEXT NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (92);

COMMIT;
