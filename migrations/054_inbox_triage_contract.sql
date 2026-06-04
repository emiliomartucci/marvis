-- v1.0.0 - 2026-04-07 - InboxX MVP triage contract sidecar table

CREATE TABLE IF NOT EXISTS inbox_triage_decisions (
    id TEXT PRIMARY KEY,
    inbox_item_id TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('ignore', 'keep', 'needs_human_review', 'create_idea', 'create_task')),
    reason TEXT NOT NULL,
    confidence REAL NOT NULL,
    target_program TEXT,
    target_project TEXT,
    task_kind TEXT CHECK (task_kind IN ('idea', 'normal')),
    task_title TEXT,
    task_description TEXT,
    linked_task_id TEXT,
    tags_json TEXT NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL,
    workspace_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(inbox_item_id, workspace_id)
);

CREATE INDEX IF NOT EXISTS idx_inbox_triage_ws_created
    ON inbox_triage_decisions(workspace_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_inbox_triage_linked_task
    ON inbox_triage_decisions(linked_task_id)
    WHERE linked_task_id IS NOT NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (54);
