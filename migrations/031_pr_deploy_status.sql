-- 031_pr_deploy_status.sql
-- v1.0.0 - 2026-03-10 - Add deploy tracking to pull_requests
-- Tracks deploy result after PR merge: status, output, timestamp

ALTER TABLE pull_requests ADD COLUMN deploy_status TEXT;  -- 'success', 'failed', 'skipped'
ALTER TABLE pull_requests ADD COLUMN deploy_output TEXT;  -- last 2000 chars of stdout+stderr
ALTER TABLE pull_requests ADD COLUMN deploy_at TEXT;      -- ISO timestamp of deploy completion

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (31, datetime('now', 'utc'));
