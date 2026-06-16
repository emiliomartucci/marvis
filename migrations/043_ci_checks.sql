-- Migration 043: CI/CD feedback loop — check tracking + merge gate
-- Tracks CI check status per task, blocks merge on required check failure

CREATE TABLE IF NOT EXISTS ci_checks (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','running','success','failure','error','skipped')),
    details_url TEXT,
    output_summary TEXT,
    started_at DATETIME,
    completed_at DATETIME,
    attempt INTEGER DEFAULT 1,
    workspace_id TEXT,
    delivery_id TEXT UNIQUE,  -- GitHub X-GitHub-Delivery UUID for dedup
    created_at DATETIME DEFAULT (datetime('now','utc')),
    UNIQUE(task_id, check_name, attempt)
);

CREATE INDEX IF NOT EXISTS idx_ci_checks_task ON ci_checks(task_id, status);
CREATE INDEX IF NOT EXISTS idx_ci_checks_ws ON ci_checks(workspace_id);

-- Project-level required checks configuration
CREATE TABLE IF NOT EXISTS project_ci_config (
    project TEXT PRIMARY KEY,
    required_checks TEXT DEFAULT '[]',  -- JSON array of check names required to pass before merge
    workspace_id TEXT,
    updated_at DATETIME DEFAULT (datetime('now','utc'))
);

INSERT OR IGNORE INTO schema_versions (version) VALUES (43);
