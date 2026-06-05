-- Agents registry
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

CREATE TABLE IF NOT EXISTS agent_sync_state (
    agent_id TEXT NOT NULL,
    run_source TEXT NOT NULL,
    last_offset INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    PRIMARY KEY (agent_id, run_source)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_agents_user_id ON agents(user_id);
CREATE INDEX IF NOT EXISTS idx_agents_project_slug ON agents(project_slug);
CREATE INDEX IF NOT EXISTS idx_agents_active ON agents(deleted_at) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_id ON agent_runs(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_runs_agent_started ON agent_runs(agent_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_runs_status ON agent_runs(agent_id, status, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_schedules_agent_id ON agent_schedules(agent_id);
CREATE INDEX IF NOT EXISTS idx_cost_runs_agent ON agent_runs(agent_id, cost_usd);

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (18, datetime('now', 'utc'));
