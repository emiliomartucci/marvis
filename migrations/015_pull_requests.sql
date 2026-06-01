-- 015_pull_requests.sql
-- v1.0.0 - 2026-02-27 - Pull requests linked to tasks (worktree-based workflow)
-- NOTE: project slug references programs.yaml / .task files (no FK by design).
-- If a project is renamed: UPDATE pull_requests SET project = 'new-slug' WHERE project = 'old-slug';

CREATE TABLE IF NOT EXISTS pull_requests (
    id            TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    project       TEXT NOT NULL,
    branch        TEXT NOT NULL,
    target        TEXT NOT NULL DEFAULT 'main',
    -- draft: worktree created, not yet submitted
    -- open: submitted, awaiting human review
    -- merging: merge in progress (lock for race condition)
    -- merged: successfully merged
    -- closed: closed without merge
    status        TEXT NOT NULL DEFAULT 'draft'
                  CHECK (status IN ('draft', 'open', 'merging', 'merged', 'closed')),
    title         TEXT,
    body          TEXT,
    worktree_path TEXT,
    closed_reason TEXT,
    merged_at     TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Base indexes
CREATE INDEX IF NOT EXISTS idx_pr_task_id ON pull_requests(task_id);
CREATE INDEX IF NOT EXISTS idx_pr_project ON pull_requests(project);
CREATE INDEX IF NOT EXISTS idx_pr_status  ON pull_requests(status);

-- One active PR (draft, open, or merging) per task at a time
-- Allows multiple historical PRs (merged, closed) for rework tracking
CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_one_active_per_task
    ON pull_requests(task_id)
    WHERE status IN ('draft', 'open', 'merging');

-- A branch cannot be in use by two active PRs simultaneously
CREATE UNIQUE INDEX IF NOT EXISTS idx_pr_branch_active
    ON pull_requests(branch)
    WHERE status IN ('draft', 'open', 'merging');

INSERT OR IGNORE INTO schema_versions (version) VALUES (15);
