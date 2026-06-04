-- 024_task_indexes.sql: Add indexes for common query patterns
-- owner_id: used in task list filtering and RACI lookups
CREATE INDEX IF NOT EXISTS idx_tasks_owner_id ON tasks(owner_id) WHERE deleted_at IS NULL;

-- project + status: the most common query pattern (list tasks by project and status)
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status) WHERE deleted_at IS NULL;

-- ice_score: used for priority sorting in task lists
CREATE INDEX IF NOT EXISTS idx_tasks_ice_score ON tasks(ice_score DESC) WHERE deleted_at IS NULL;

-- delegation: used for filtering by delegation type
CREATE INDEX IF NOT EXISTS idx_tasks_delegation ON tasks(delegation) WHERE deleted_at IS NULL AND delegation IS NOT NULL;

-- Pull requests: task_id lookup (used in every PR operation)
CREATE INDEX IF NOT EXISTS idx_pull_requests_task_id ON pull_requests(task_id);

-- Session costs: project_slug + date range (used in cost summary)
CREATE INDEX IF NOT EXISTS idx_session_costs_project_date ON session_costs(project_slug, updated_at);

-- Task cost entries: task_id (used in cost summary per task)
CREATE INDEX IF NOT EXISTS idx_task_cost_entries_task_id ON task_cost_entries(task_id);
