-- Rollback Migration 084: Drop legacy scheduler tables
--
-- NOTE: structure-only rollback. Data is NOT restored.
-- For data recovery, restore from console.db.backup-v83 (taken by the migration runner
-- before applying 084) — that backup contains the full pre-migration state.
--
-- Schemas below are reproduced verbatim from migrations 018 (agents, agent_schedules,
-- agent_runs, agent_sync_state) and 044 (agent_daily_metrics). Keep in sync with those
-- if future changes land.

PRAGMA foreign_keys = OFF;

-- agents (from 018_agents.sql)
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
    scheduler_agent_id TEXT UNIQUE,
    agent_type TEXT NOT NULL DEFAULT 'system' CHECK(agent_type IN ('project', 'system', 'digital_copy')),
    project_slug TEXT,
    model TEXT NOT NULL DEFAULT 'haiku' CHECK(model IN ('haiku', 'sonnet', 'opus')),
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active', 'inactive', 'error')),
    enabled INTEGER NOT NULL DEFAULT 1,
    soul_path TEXT,
    tools_path TEXT,
    identity_path TEXT,
    description TEXT,
    deleted_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);
CREATE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_project_slug ON agents(project_slug);
CREATE INDEX IF NOT EXISTS idx_agents_active ON agents(deleted_at) WHERE deleted_at IS NULL;

-- agent_schedules (from 018_agents.sql)
CREATE TABLE IF NOT EXISTS agent_schedules (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    cron_expr TEXT NOT NULL,
    cron_tz TEXT NOT NULL DEFAULT 'Europe/Rome',
    prompt TEXT,
    timeout_seconds INTEGER NOT NULL DEFAULT 120,
    enabled INTEGER NOT NULL DEFAULT 1,
    scheduler_job_id TEXT UNIQUE,
    last_run_at TEXT,
    last_run_status TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);
CREATE INDEX IF NOT EXISTS idx_agent_schedules_agent_id ON agent_schedules(agent_id);

-- agent_runs (from 018_agents.sql)
CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE RESTRICT,
    schedule_id TEXT REFERENCES agent_schedules(id) ON DELETE SET NULL,
    trigger TEXT NOT NULL DEFAULT 'manual' CHECK(trigger IN ('cron', 'manual', 'api')),
    session_uuid TEXT,
    started_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running' CHECK(status IN ('running', 'success', 'error', 'timeout', 'killed')),
    summary TEXT,
    cost_usd REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    model TEXT,
    log_tail TEXT,
    log_size_bytes INTEGER,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_id ON agent_runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_started ON agent_runs(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(agent_id, status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_cost_runs_agent ON agent_runs(agent_id, cost_usd);

-- agent_sync_state (from 018_agents.sql)
CREATE TABLE IF NOT EXISTS agent_sync_state (
    agent_id TEXT NOT NULL,
    run_source TEXT NOT NULL,
    last_offset INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    PRIMARY KEY (agent_id, run_source)
);

-- agent_daily_metrics (from 044_agent_metrics.sql)
CREATE TABLE IF NOT EXISTS agent_daily_metrics (
    id TEXT PRIMARY KEY,
    agent_slug TEXT NOT NULL,
    workspace_id TEXT DEFAULT 'ws_default',
    metric_date TEXT NOT NULL,
    tasks_created INTEGER DEFAULT 0,
    tasks_completed INTEGER DEFAULT 0,
    tasks_failed INTEGER DEFAULT 0,
    avg_duration_minutes REAL,
    total_cost_usd REAL DEFAULT 0.0,
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    ci_passes INTEGER DEFAULT 0,
    ci_failures INTEGER DEFAULT 0,
    prs_submitted INTEGER DEFAULT 0,
    prs_merged INTEGER DEFAULT 0,
    prs_rejected INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT (datetime('now','utc')),
    UNIQUE(agent_slug, workspace_id, metric_date)
);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_ws_date ON agent_daily_metrics(workspace_id, metric_date);
CREATE INDEX IF NOT EXISTS idx_agent_metrics_agent ON agent_daily_metrics(agent_slug, metric_date);

PRAGMA foreign_keys = ON;

DELETE FROM schema_versions WHERE version = 84;
