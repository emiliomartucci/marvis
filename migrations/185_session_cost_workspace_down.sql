-- Down migration 185.  The copy into the old fleet-global primary key is the
-- rollback safety gate: if two workspaces now use the same conversation_id,
-- the INSERT aborts before any live table is dropped and the v185 schema stays
-- intact after rollback.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

-- A downgrade cannot restore quarantined rows to the fleet-global billing
-- table without reviving the ownership ambiguity.  Old v185 databases did not
-- create this table, so an empty compatibility shell keeps their rollback
-- lossless while any real quarantine remains a hard stop.
CREATE TABLE IF NOT EXISTS task_cost_entries_v185_quarantine (
    id TEXT PRIMARY KEY
);
CREATE TEMP TABLE v185_quarantine_rollback_gate (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO v185_quarantine_rollback_gate(ok)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM task_cost_entries_v185_quarantine
) THEN 0 ELSE 1 END;
DROP TABLE v185_quarantine_rollback_gate;

CREATE TABLE session_costs_v185_down (
    conversation_id TEXT PRIMARY KEY,
    session_name TEXT,
    project_slug TEXT,
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_write_tokens INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    message_count INTEGER DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT
);

INSERT INTO session_costs_v185_down (
    conversation_id, session_name, project_slug, model, input_tokens,
    output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
    message_count, updated_at, completed_at
)
SELECT
    conversation_id, session_name, project_slug, model, input_tokens,
    output_tokens, cache_read_tokens, cache_write_tokens, cost_usd,
    message_count, updated_at, completed_at
FROM session_costs;

CREATE TABLE session_conversations_v185_down (
    session_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_name, conversation_id),
    FOREIGN KEY (session_name) REFERENCES sessions_meta(name) ON DELETE CASCADE
);

INSERT INTO session_conversations_v185_down (
    session_name, conversation_id, ord, created_at
)
SELECT session_name, conversation_id, ord, created_at
FROM session_conversations;

CREATE TABLE pull_requests_v185_down (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    project TEXT NOT NULL,
    branch TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT 'main',
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft', 'open', 'merging', 'merged', 'closed')),
    title TEXT,
    body TEXT,
    worktree_path TEXT,
    closed_reason TEXT,
    merged_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    commit_sha TEXT,
    conversation_id TEXT REFERENCES session_costs(conversation_id)
        ON DELETE SET NULL,
    deploy_status TEXT,
    deploy_output TEXT,
    deploy_at TEXT,
    approved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    approved_at DATETIME,
    submitted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    workspace_id TEXT
);

INSERT INTO pull_requests_v185_down (
    id, task_id, project, branch, target, status, title, body,
    worktree_path, closed_reason, merged_at, created_at, commit_sha,
    conversation_id, deploy_status, deploy_output, deploy_at, approved_by,
    approved_at, submitted_by, workspace_id
)
SELECT
    id, task_id, project, branch, target, status, title, body,
    worktree_path, closed_reason, merged_at, created_at, commit_sha,
    conversation_id, deploy_status, deploy_output, deploy_at, approved_by,
    approved_at, submitted_by, workspace_id
FROM pull_requests;

CREATE TABLE task_cost_entries_v185_down (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('agent', 'human')),
    source TEXT NOT NULL CHECK (source IN ('task_completed', 'manual')),
    conversation_id TEXT REFERENCES session_costs(conversation_id)
        ON DELETE SET NULL,
    pr_id TEXT REFERENCES pull_requests(id) ON DELETE SET NULL,
    cost_usd_delta REAL NOT NULL DEFAULT 0.0 CHECK (cost_usd_delta >= 0.0),
    token_markup_factor REAL NOT NULL DEFAULT 1.0
        CHECK (token_markup_factor >= 1.0),
    agent_seconds INTEGER NOT NULL DEFAULT 0 CHECK (agent_seconds >= 0),
    agent_cost_rate REAL NOT NULL DEFAULT 0.0 CHECK (agent_cost_rate >= 0.0),
    agent_bill_rate REAL NOT NULL DEFAULT 0.0 CHECK (agent_bill_rate >= 0.0),
    human_minutes REAL NOT NULL DEFAULT 0.0 CHECK (human_minutes >= 0.0),
    human_cost_rate REAL NOT NULL DEFAULT 0.0 CHECK (human_cost_rate >= 0.0),
    human_bill_rate REAL NOT NULL DEFAULT 0.0 CHECK (human_bill_rate >= 0.0),
    total_cost_usd REAL NOT NULL DEFAULT 0.0 CHECK (total_cost_usd >= 0.0),
    total_bill_usd REAL NOT NULL DEFAULT 0.0 CHECK (total_bill_usd >= 0.0),
    is_billable INTEGER NOT NULL DEFAULT 1,
    billable_reason TEXT,
    billing_notes TEXT,
    idempotency_key TEXT,
    description TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    CHECK (entry_type != 'agent' OR conversation_id IS NOT NULL),
    CHECK (entry_type != 'human' OR human_minutes > 0.0),
    CHECK (entry_type != 'agent' OR human_minutes = 0.0),
    CHECK (entry_type != 'human' OR cost_usd_delta = 0.0)
);

INSERT INTO task_cost_entries_v185_down (
    id, task_id, entry_type, source, conversation_id, pr_id,
    cost_usd_delta, token_markup_factor, agent_seconds, agent_cost_rate,
    agent_bill_rate, human_minutes, human_cost_rate, human_bill_rate,
    total_cost_usd, total_bill_usd, is_billable, billable_reason,
    billing_notes, idempotency_key, description, created_by, created_at
)
SELECT
    id, task_id, entry_type, source, conversation_id, pr_id,
    cost_usd_delta, token_markup_factor, agent_seconds, agent_cost_rate,
    agent_bill_rate, human_minutes, human_cost_rate, human_bill_rate,
    total_cost_usd, total_bill_usd, is_billable, billable_reason,
    billing_notes, idempotency_key, description, created_by, created_at
FROM task_cost_entries;

DROP TRIGGER IF EXISTS pull_requests_conversation_workspace_insert;
DROP TRIGGER IF EXISTS pull_requests_conversation_workspace_update;
DROP TABLE task_cost_entries;
DROP TABLE session_conversations;

PRAGMA legacy_alter_table=ON;
ALTER TABLE session_costs RENAME TO session_costs_v185_current;
ALTER TABLE session_costs_v185_down RENAME TO session_costs;
DROP TABLE session_costs_v185_current;
PRAGMA legacy_alter_table=OFF;
ALTER TABLE session_conversations_v185_down RENAME TO session_conversations;
PRAGMA legacy_alter_table=ON;
ALTER TABLE pull_requests RENAME TO pull_requests_v185_current;
ALTER TABLE pull_requests_v185_down RENAME TO pull_requests;
DROP TABLE pull_requests_v185_current;
PRAGMA legacy_alter_table=OFF;
ALTER TABLE task_cost_entries_v185_down RENAME TO task_cost_entries;

DROP INDEX IF EXISTS idx_sessions_workspace_name_unique;

CREATE INDEX idx_session_costs_project_updated
    ON session_costs(project_slug, updated_at);
CREATE INDEX idx_session_costs_project_date
    ON session_costs(project_slug, updated_at);
CREATE INDEX idx_session_costs_completed
    ON session_costs(completed_at)
    WHERE completed_at IS NOT NULL;
CREATE INDEX idx_session_conv_name
    ON session_conversations(session_name);
CREATE INDEX idx_session_conv_id
    ON session_conversations(conversation_id);

CREATE INDEX idx_task_cost_entries_task_id ON task_cost_entries(task_id);
CREATE INDEX idx_tce_task_cost
    ON task_cost_entries(
        task_id, is_billable, entry_type, total_cost_usd, total_bill_usd
    );
CREATE INDEX idx_tce_conversation_agent
    ON task_cost_entries(conversation_id, cost_usd_delta)
    WHERE entry_type = 'agent' AND conversation_id IS NOT NULL;
CREATE INDEX idx_tce_created_at_cost
    ON task_cost_entries(
        created_at, task_id, total_cost_usd, total_bill_usd,
        entry_type, is_billable
    );
CREATE UNIQUE INDEX idx_tce_agent_idempotency
    ON task_cost_entries(task_id, conversation_id, source)
    WHERE conversation_id IS NOT NULL;
CREATE UNIQUE INDEX idx_tce_human_idempotency
    ON task_cost_entries(task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

CREATE TRIGGER tce_no_update
BEFORE UPDATE ON task_cost_entries
BEGIN
  SELECT RAISE(ABORT, 'task_cost_entries is append-only');
END;
CREATE TRIGGER tce_no_delete
BEFORE DELETE ON task_cost_entries
BEGIN
  SELECT RAISE(ABORT, 'task_cost_entries is append-only');
END;

CREATE INDEX idx_pr_task_id ON pull_requests(task_id);
CREATE INDEX idx_pull_requests_task_id ON pull_requests(task_id);
CREATE INDEX idx_pr_project ON pull_requests(project);
CREATE INDEX idx_pr_status ON pull_requests(status);
CREATE INDEX idx_prs_ws ON pull_requests(workspace_id);
CREATE INDEX idx_prs_workspace_task_status_created
    ON pull_requests(workspace_id, task_id, status, created_at DESC);
CREATE INDEX idx_prs_workspace_project_status_created
    ON pull_requests(workspace_id, project, status, created_at);
CREATE UNIQUE INDEX idx_pr_one_active_per_task
    ON pull_requests(task_id)
    WHERE status IN ('draft', 'open', 'merging');
CREATE UNIQUE INDEX idx_pr_branch_active
    ON pull_requests(branch)
    WHERE status IN ('draft', 'open', 'merging');

DROP TABLE task_cost_entries_v185_quarantine;

DELETE FROM schema_versions WHERE version = 185;

COMMIT;
PRAGMA foreign_keys=ON;
