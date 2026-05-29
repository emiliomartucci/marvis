-- Migration 033: Add retry_count to events table for n8n dispatcher
-- Tracks dispatch attempts per event. Dead letter after n8n_max_retry_count failures.

ALTER TABLE events ADD COLUMN retry_count INTEGER DEFAULT 0;

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (33, datetime('now', 'utc'));
