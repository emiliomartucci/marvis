-- 020_pr_commit_sha.sql
-- v1.0.0 - 2026-03-01 - Add commit_sha to pull_requests for revert support
ALTER TABLE pull_requests ADD COLUMN commit_sha TEXT;
INSERT OR IGNORE INTO schema_versions (version) VALUES (20);
