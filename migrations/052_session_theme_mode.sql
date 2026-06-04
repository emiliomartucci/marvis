-- v052 - 2026-04-09 - Persist requested theme mode for OpenCode relaunches
ALTER TABLE sessions_meta ADD COLUMN theme_mode TEXT DEFAULT NULL;
