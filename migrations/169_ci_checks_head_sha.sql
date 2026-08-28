-- Migration 169: bind required CI results to the exact task-branch commit.

ALTER TABLE ci_checks ADD COLUMN head_sha TEXT;

CREATE INDEX IF NOT EXISTS idx_ci_checks_task_head
    ON ci_checks(task_id, head_sha, status);

INSERT OR IGNORE INTO schema_versions (version) VALUES (169);
