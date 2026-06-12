-- Migration 044: Agent performance metrics — materialized view table + event hooks
-- Task completion rate, avg time, failure analysis, cost-per-task
-- Data is aggregated from existing tables (tasks, session_costs, ci_checks)

-- Materialized metrics snapshot per agent per day (refreshed by periodic job)
CREATE TABLE IF NOT EXISTS agent_daily_metrics (
    id TEXT PRIMARY KEY,
    agent_slug TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'ws_default',
    metric_date TEXT NOT NULL,  -- YYYY-MM-DD
    tasks_created INTEGER DEFAULT 0,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,  -- tasks that went back to in_progress from review
    avg_duration_minutes REAL,  -- avg time from in_progress to completed
    total_cost_usd REAL DEFAULT 0.0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    ci_passes INTEGER DEFAULT 0,
    ci_failures INTEGER DEFAULT 0,
    prs_submitted INTEGER DEFAULT 0,
    prs_merged INTEGER DEFAULT 0,
    prs_rejected INTEGER DEFAULT 0,  -- changes_requested or closed
    created_at DATETIME DEFAULT (datetime('now','utc')),
    UNIQUE(agent_slug, workspace_id, metric_date)
);

CREATE INDEX IF NOT EXISTS idx_agent_metrics_ws_date ON agent_daily_metrics(workspace_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent ON agent_daily_metrics(agent_slug, metric_date);

INSERT OR IGNORE INTO schema_versions (version) VALUES (44);
