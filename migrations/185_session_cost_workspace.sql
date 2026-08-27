-- Migration 185: make persisted session costs and resume chains tenant-safe.
--
-- The old natural key was fleet-global `conversation_id`.  The new identity is
-- `(workspace_id, conversation_id)` so two tenants may legitimately use the
-- same provider identifier.  Historical ownership is copied only from an
-- exact sessions_meta parent with a non-empty workspace.  Rows without that
-- proof retain workspace_id NULL and remain inert behind the write guards.
--
-- task_cost_entries and pull_requests used a foreign key to the obsolete
-- global key.  They are rebuilt without that invalid reference and receive an
-- exact-workspace trigger instead; all their other columns and constraints are
-- preserved.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

-- Stop before any table rebuild if an existing task-cost conversation cannot
-- be proven to have the same workspace as its task. Silent copying (or NULLing)
-- would turn an ownership mismatch into trusted billing history.
CREATE TEMP TABLE v185_task_cost_workspace_gate (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO v185_task_cost_workspace_gate(ok)
SELECT CASE WHEN EXISTS (
    SELECT 1
      FROM task_cost_entries tce
      JOIN tasks task ON task.id = tce.task_id
      LEFT JOIN session_costs sc
        ON sc.conversation_id = tce.conversation_id
     WHERE tce.conversation_id IS NOT NULL
       AND (
           task.workspace_id IS NULL
           OR length(trim(task.workspace_id)) = 0
           OR sc.conversation_id IS NULL
           OR (
               SELECT COUNT(DISTINCT sm.workspace_id)
                 FROM sessions_meta sm
                WHERE sm.name = sc.session_name
                  AND sm.workspace_id IS NOT NULL
                  AND length(trim(sm.workspace_id)) > 0
           ) != 1
           OR NOT EXISTS (
               SELECT 1
                 FROM sessions_meta sm
                WHERE sm.name = sc.session_name
                  AND sm.workspace_id = task.workspace_id
           )
       )
) THEN 0 ELSE 1 END;
DROP TABLE v185_task_cost_workspace_gate;

CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_workspace_name_unique
    ON sessions_meta(workspace_id, name);

-- -------------------------------------------------------------------------
-- session_costs
-- -------------------------------------------------------------------------

CREATE TABLE session_costs_v185_new (
    workspace_id TEXT,
    conversation_id TEXT NOT NULL,
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
    completed_at TEXT,
    PRIMARY KEY (workspace_id, conversation_id)
);

INSERT INTO session_costs_v185_new (
    workspace_id, conversation_id, session_name, project_slug, model,
    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
    cost_usd, message_count, updated_at, completed_at
)
SELECT
    CASE
      WHEN sc.session_name IS NOT NULL
       AND (
          SELECT COUNT(DISTINCT sm.workspace_id)
            FROM sessions_meta sm
           WHERE sm.name = sc.session_name
             AND sm.workspace_id IS NOT NULL
             AND length(trim(sm.workspace_id)) > 0
       ) = 1
      THEN (
          SELECT MIN(sm.workspace_id)
            FROM sessions_meta sm
           WHERE sm.name = sc.session_name
             AND sm.workspace_id IS NOT NULL
             AND length(trim(sm.workspace_id)) > 0
       )
      ELSE NULL
    END,
    sc.conversation_id, sc.session_name, sc.project_slug, sc.model,
    sc.input_tokens, sc.output_tokens, sc.cache_read_tokens,
    sc.cache_write_tokens, sc.cost_usd, sc.message_count, sc.updated_at,
    sc.completed_at
FROM session_costs sc;

DROP TABLE session_costs;
ALTER TABLE session_costs_v185_new RENAME TO session_costs;

CREATE INDEX idx_session_costs_workspace_project_updated
    ON session_costs(workspace_id, project_slug, updated_at)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_session_costs_workspace_session
    ON session_costs(workspace_id, session_name, updated_at)
    WHERE workspace_id IS NOT NULL AND session_name IS NOT NULL;
CREATE INDEX idx_session_costs_workspace_completed
    ON session_costs(workspace_id, completed_at)
    WHERE workspace_id IS NOT NULL AND completed_at IS NOT NULL;
CREATE INDEX idx_session_costs_conversation_lookup
    ON session_costs(conversation_id);

CREATE TRIGGER session_costs_workspace_required_insert
BEFORE INSERT ON session_costs
WHEN (NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0)
 AND (
      SELECT COUNT(DISTINCT sm.workspace_id)
        FROM sessions_meta sm
       WHERE sm.name = NEW.session_name
         AND sm.workspace_id IS NOT NULL
         AND length(trim(sm.workspace_id)) > 0
 ) != 1
BEGIN
  SELECT RAISE(ABORT, 'session cost workspace_id required');
END;

CREATE TRIGGER session_costs_parent_workspace_insert
BEFORE INSERT ON session_costs
WHEN NEW.workspace_id IS NOT NULL
 AND length(trim(NEW.workspace_id)) > 0
 AND NOT EXISTS (
      SELECT 1
        FROM sessions_meta sm
       WHERE sm.name = NEW.session_name
         AND sm.workspace_id = NEW.workspace_id
 )
BEGIN
  SELECT RAISE(ABORT, 'session cost parent workspace mismatch');
END;

CREATE TRIGGER session_costs_workspace_derive_insert
AFTER INSERT ON session_costs
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  UPDATE session_costs
     SET workspace_id = (
         SELECT MIN(sm.workspace_id)
           FROM sessions_meta sm
          WHERE sm.name = NEW.session_name
            AND sm.workspace_id IS NOT NULL
            AND length(trim(sm.workspace_id)) > 0
     )
   WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER session_costs_workspace_required_update
BEFORE UPDATE ON session_costs
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'session cost workspace_id required');
END;

CREATE TRIGGER session_costs_workspace_immutable
BEFORE UPDATE OF workspace_id ON session_costs
WHEN OLD.workspace_id IS NOT NULL
 AND length(trim(OLD.workspace_id)) > 0
 AND OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'session cost workspace_id immutable');
END;

CREATE TRIGGER session_costs_parent_workspace_update
BEFORE UPDATE OF workspace_id, session_name ON session_costs
WHEN NEW.session_name IS NOT NULL
 AND NOT EXISTS (
      SELECT 1
        FROM sessions_meta sm
       WHERE sm.name = NEW.session_name
         AND sm.workspace_id = NEW.workspace_id
 )
BEGIN
  SELECT RAISE(ABORT, 'session cost parent workspace mismatch');
END;

CREATE TRIGGER session_costs_historical_attribution_guard
BEFORE UPDATE OF workspace_id ON session_costs
WHEN (OLD.workspace_id IS NULL OR length(trim(OLD.workspace_id)) = 0)
 AND NEW.workspace_id IS NOT NULL
 AND length(trim(NEW.workspace_id)) > 0
 AND (
      NEW.session_name IS NULL
      OR NOT EXISTS (
          SELECT 1
            FROM sessions_meta sm
           WHERE sm.name = NEW.session_name
             AND sm.workspace_id = NEW.workspace_id
      )
 )
BEGIN
  SELECT RAISE(ABORT, 'session cost parent workspace mismatch');
END;

-- -------------------------------------------------------------------------
-- session_conversations
-- -------------------------------------------------------------------------

CREATE TABLE session_conversations_v185_new (
    workspace_id TEXT,
    session_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, session_name, conversation_id),
    FOREIGN KEY (workspace_id, session_name)
        REFERENCES sessions_meta(workspace_id, name)
        ON DELETE CASCADE ON UPDATE CASCADE
);

INSERT INTO session_conversations_v185_new (
    workspace_id, session_name, conversation_id, ord, created_at
)
SELECT
    CASE
      WHEN (
          SELECT COUNT(DISTINCT sm.workspace_id)
            FROM sessions_meta sm
           WHERE sm.name = sc.session_name
             AND sm.workspace_id IS NOT NULL
             AND length(trim(sm.workspace_id)) > 0
      ) = 1
      THEN (
          SELECT MIN(sm.workspace_id)
            FROM sessions_meta sm
           WHERE sm.name = sc.session_name
             AND sm.workspace_id IS NOT NULL
             AND length(trim(sm.workspace_id)) > 0
      )
      ELSE NULL
    END,
    sc.session_name, sc.conversation_id, sc.ord, sc.created_at
FROM session_conversations sc;

DROP TABLE session_conversations;
ALTER TABLE session_conversations_v185_new RENAME TO session_conversations;

CREATE UNIQUE INDEX idx_session_conversations_workspace_ord
    ON session_conversations(workspace_id, session_name, ord)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_session_conversations_workspace_name
    ON session_conversations(workspace_id, session_name, ord)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_session_conversations_workspace_conversation
    ON session_conversations(workspace_id, conversation_id)
    WHERE workspace_id IS NOT NULL;

CREATE TRIGGER session_conversations_duplicate_insert
BEFORE INSERT ON session_conversations
WHEN EXISTS (
    SELECT 1
      FROM session_conversations existing
     WHERE existing.workspace_id = COALESCE(
               NULLIF(trim(NEW.workspace_id), ''),
               (
                 SELECT MIN(sm.workspace_id)
                   FROM sessions_meta sm
                  WHERE sm.name = NEW.session_name
                    AND sm.workspace_id IS NOT NULL
                    AND length(trim(sm.workspace_id)) > 0
               )
           )
       AND existing.session_name = NEW.session_name
       AND existing.conversation_id = NEW.conversation_id
)
BEGIN
  SELECT RAISE(IGNORE);
END;

CREATE TRIGGER session_conversations_workspace_required_insert
BEFORE INSERT ON session_conversations
WHEN (NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0)
 AND (
      SELECT COUNT(DISTINCT sm.workspace_id)
        FROM sessions_meta sm
       WHERE sm.name = NEW.session_name
         AND sm.workspace_id IS NOT NULL
         AND length(trim(sm.workspace_id)) > 0
 ) != 1
BEGIN
  SELECT RAISE(ABORT, 'session conversation workspace_id required');
END;

CREATE TRIGGER session_conversations_parent_workspace_insert
BEFORE INSERT ON session_conversations
WHEN NEW.workspace_id IS NOT NULL
 AND length(trim(NEW.workspace_id)) > 0
 AND NOT EXISTS (
      SELECT 1
        FROM sessions_meta sm
       WHERE sm.name = NEW.session_name
         AND sm.workspace_id = NEW.workspace_id
 )
BEGIN
  SELECT RAISE(ABORT, 'session conversation parent workspace mismatch');
END;

CREATE TRIGGER session_conversations_workspace_derive_insert
AFTER INSERT ON session_conversations
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  UPDATE session_conversations
     SET workspace_id = (
         SELECT MIN(sm.workspace_id)
           FROM sessions_meta sm
          WHERE sm.name = NEW.session_name
            AND sm.workspace_id IS NOT NULL
            AND length(trim(sm.workspace_id)) > 0
     )
   WHERE rowid = NEW.rowid;
END;

CREATE TRIGGER session_conversations_workspace_required_update
BEFORE UPDATE ON session_conversations
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'session conversation workspace_id required');
END;

CREATE TRIGGER session_conversations_workspace_immutable
BEFORE UPDATE OF workspace_id ON session_conversations
WHEN OLD.workspace_id IS NOT NULL
 AND length(trim(OLD.workspace_id)) > 0
 AND OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'session conversation workspace_id immutable');
END;

CREATE TRIGGER session_conversations_parent_workspace_update
BEFORE UPDATE OF workspace_id, session_name ON session_conversations
WHEN NOT EXISTS (
    SELECT 1
      FROM sessions_meta sm
     WHERE sm.name = NEW.session_name
       AND sm.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'session conversation parent workspace mismatch');
END;

-- -------------------------------------------------------------------------
-- Remove child foreign keys to the obsolete global conversation key.  Exact
-- workspace ownership is enforced by the insert/update guards below.
-- -------------------------------------------------------------------------

DROP TRIGGER IF EXISTS tce_no_update;
DROP TRIGGER IF EXISTS tce_no_delete;

CREATE TABLE task_cost_entries_v185_new (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    entry_type TEXT NOT NULL CHECK (entry_type IN ('agent', 'human')),
    source TEXT NOT NULL CHECK (source IN ('task_completed', 'manual')),
    conversation_id TEXT,
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

INSERT INTO task_cost_entries_v185_new (
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

DROP TABLE task_cost_entries;
ALTER TABLE task_cost_entries_v185_new RENAME TO task_cost_entries;

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

CREATE TRIGGER task_cost_entries_conversation_workspace_insert
BEFORE INSERT ON task_cost_entries
WHEN NEW.conversation_id IS NOT NULL
 AND NOT EXISTS (
      SELECT 1
        FROM tasks t
        JOIN session_costs sc
          ON sc.workspace_id = t.workspace_id
         AND sc.conversation_id = NEW.conversation_id
       WHERE t.id = NEW.task_id
         AND t.workspace_id IS NOT NULL
         AND length(trim(t.workspace_id)) > 0
 )
BEGIN
  SELECT RAISE(ABORT, 'task cost conversation workspace mismatch');
END;

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

CREATE TABLE pull_requests_v185_new (
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
    conversation_id TEXT,
    deploy_status TEXT,
    deploy_output TEXT,
    deploy_at TEXT,
    approved_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    approved_at DATETIME,
    submitted_by TEXT REFERENCES users(id) ON DELETE SET NULL,
    workspace_id TEXT
);

INSERT INTO pull_requests_v185_new (
    id, task_id, project, branch, target, status, title, body,
    worktree_path, closed_reason, merged_at, created_at, commit_sha,
    conversation_id, deploy_status, deploy_output, deploy_at, approved_by,
    approved_at, submitted_by, workspace_id
)
SELECT
    id, task_id, project, branch, target, status, title, body,
    worktree_path, closed_reason, merged_at, created_at, commit_sha,
    CASE
      WHEN conversation_id IS NULL
        OR EXISTS (
            SELECT 1
              FROM session_costs sc
             WHERE sc.workspace_id = pull_requests.workspace_id
               AND sc.conversation_id = pull_requests.conversation_id
        )
      THEN conversation_id
      ELSE NULL
    END,
    deploy_status, deploy_output, deploy_at, approved_by,
    approved_at, submitted_by, workspace_id
FROM pull_requests;

-- Keep triggers on other tables that reference pull_requests intact.  With
-- legacy_alter_table enabled their SQL continues to reference the canonical
-- name while the old and new tables are swapped without a name collision.
PRAGMA legacy_alter_table=ON;
ALTER TABLE pull_requests RENAME TO pull_requests_v185_old;
ALTER TABLE pull_requests_v185_new RENAME TO pull_requests;
DROP TABLE pull_requests_v185_old;
PRAGMA legacy_alter_table=OFF;

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

CREATE TRIGGER pull_requests_conversation_workspace_insert
BEFORE INSERT ON pull_requests
WHEN NEW.conversation_id IS NOT NULL
 AND NOT EXISTS (
      SELECT 1
        FROM session_costs sc
       WHERE sc.workspace_id = NEW.workspace_id
         AND sc.conversation_id = NEW.conversation_id
 )
BEGIN
  SELECT RAISE(ABORT, 'pull request cost workspace mismatch');
END;

CREATE TRIGGER pull_requests_conversation_workspace_update
BEFORE UPDATE OF workspace_id, conversation_id ON pull_requests
WHEN NEW.conversation_id IS NOT NULL
 AND NOT EXISTS (
      SELECT 1
        FROM session_costs sc
       WHERE sc.workspace_id = NEW.workspace_id
         AND sc.conversation_id = NEW.conversation_id
 )
BEGIN
  SELECT RAISE(ABORT, 'pull request cost workspace mismatch');
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (185);

COMMIT;
PRAGMA foreign_keys=ON;
