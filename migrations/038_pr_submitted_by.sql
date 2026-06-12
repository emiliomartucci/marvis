-- v038: Add submitted_by to pull_requests (tracks who submitted the PR for four-eyes on approve)

ALTER TABLE pull_requests ADD COLUMN submitted_by TEXT REFERENCES users(id) ON DELETE SET NULL;

INSERT OR IGNORE INTO schema_versions (version, applied_at)
VALUES (38, datetime('now'));
