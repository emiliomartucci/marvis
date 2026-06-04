-- v050 - 2026-04-01 - Add provider column to sessions_meta
ALTER TABLE sessions_meta ADD COLUMN provider TEXT DEFAULT 'claude';
