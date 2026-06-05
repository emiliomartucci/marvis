-- 017_task_cost_entries.sql
-- v1.0.0 - 2026-02-28 - Task cost entries: per-task agent and human cost tracking
-- Append-only audit log linking session costs and human time to specific tasks.
-- Supports dual-rate billing (internal cost + client price), token markup, idempotency.

CREATE TABLE IF NOT EXISTS task_cost_entries (
    id              TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    entry_type      TEXT NOT NULL CHECK (entry_type IN ('agent', 'human')),
    source          TEXT NOT NULL CHECK (source IN ('task_completed', 'manual')),

    -- Agent fields
    conversation_id TEXT REFERENCES session_costs(conversation_id) ON DELETE SET NULL,
    pr_id           TEXT REFERENCES pull_requests(id) ON DELETE SET NULL,
    cost_usd_delta  REAL NOT NULL DEFAULT 0.0 CHECK (cost_usd_delta >= 0.0),
    token_markup_factor REAL NOT NULL DEFAULT 1.0 CHECK (token_markup_factor >= 1.0),
    agent_seconds   INTEGER NOT NULL DEFAULT 0 CHECK (agent_seconds >= 0),
    agent_cost_rate REAL NOT NULL DEFAULT 0.0 CHECK (agent_cost_rate >= 0.0),
    agent_bill_rate REAL NOT NULL DEFAULT 0.0 CHECK (agent_bill_rate >= 0.0),

    -- Human fields
    human_minutes   REAL NOT NULL DEFAULT 0.0 CHECK (human_minutes >= 0.0),
    human_cost_rate REAL NOT NULL DEFAULT 0.0 CHECK (human_cost_rate >= 0.0),
    human_bill_rate REAL NOT NULL DEFAULT 0.0 CHECK (human_bill_rate >= 0.0),

    -- Computed totals (snapshot at insert — immutable)
    total_cost_usd  REAL NOT NULL DEFAULT 0.0 CHECK (total_cost_usd >= 0.0),
    total_bill_usd  REAL NOT NULL DEFAULT 0.0 CHECK (total_bill_usd >= 0.0),

    -- Billing disposition
    is_billable     INTEGER NOT NULL DEFAULT 1,
    billable_reason TEXT,
    billing_notes   TEXT,
    idempotency_key TEXT,           -- human entries: deduplication key (UNIQUE per task)

    -- Metadata
    description     TEXT,
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL DEFAULT (datetime('now', 'utc')),

    -- Type-conditional constraints (table-level, must come after all column defs)
    CHECK (entry_type != 'agent' OR conversation_id IS NOT NULL),
    CHECK (entry_type != 'human' OR human_minutes > 0.0),
    CHECK (entry_type != 'agent' OR human_minutes = 0.0),
    CHECK (entry_type != 'human' OR cost_usd_delta = 0.0)
);

-- Covering index for task cost aggregation (most frequent query)
CREATE INDEX IF NOT EXISTS idx_tce_task_cost
    ON task_cost_entries(task_id, is_billable, entry_type, total_cost_usd, total_bill_usd);

-- Partial index for agent delta lookup (only agent entries with conversation_id)
CREATE INDEX IF NOT EXISTS idx_tce_conversation_agent
    ON task_cost_entries(conversation_id, cost_usd_delta)
    WHERE entry_type = 'agent' AND conversation_id IS NOT NULL;

-- Date range index for billing period queries
CREATE INDEX IF NOT EXISTS idx_tce_created_at_cost
    ON task_cost_entries(created_at, task_id, total_cost_usd, total_bill_usd, entry_type, is_billable);

-- Ensure index on tasks.project (for project billing query)
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

-- Agent entry idempotency: one entry per (task, conversation, source)
CREATE UNIQUE INDEX IF NOT EXISTS idx_tce_agent_idempotency
    ON task_cost_entries(task_id, conversation_id, source)
    WHERE conversation_id IS NOT NULL;

-- Human entry idempotency: per task
CREATE UNIQUE INDEX IF NOT EXISTS idx_tce_human_idempotency
    ON task_cost_entries(task_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Immutability triggers (append-only audit log)
CREATE TRIGGER IF NOT EXISTS tce_no_update
BEFORE UPDATE ON task_cost_entries
BEGIN
    SELECT RAISE(ABORT, 'task_cost_entries is append-only');
END;

CREATE TRIGGER IF NOT EXISTS tce_no_delete
BEFORE DELETE ON task_cost_entries
BEGIN
    SELECT RAISE(ABORT, 'task_cost_entries is append-only');
END;

INSERT OR IGNORE INTO schema_versions (version) VALUES (17);
