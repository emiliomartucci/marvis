-- Migration 179: workspace-first indexes for authenticated shared data paths.
--
-- These indexes make the tenant boundary visible in query plans instead of
-- relying on globally unique primary keys as the first lookup key.
-- The guarded Python post-hook also adds workspace_id to access_grants,
-- backfills only unambiguous identities, and installs fail-closed triggers.

BEGIN IMMEDIATE;

CREATE INDEX IF NOT EXISTS idx_tasks_workspace_id
    ON tasks(workspace_id, id);

CREATE INDEX IF NOT EXISTS idx_prs_workspace_task_status_created
    ON pull_requests(workspace_id, task_id, status, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_prs_workspace_project_status_created
    ON pull_requests(workspace_id, project, status, created_at);

CREATE INDEX IF NOT EXISTS idx_learnings_workspace_id
    ON learnings(workspace_id, id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (179);
COMMIT;
