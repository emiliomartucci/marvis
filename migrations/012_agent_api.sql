-- 012_agent_api.sql
-- Audit log table for agent actions on sessions.
-- session_name is denormalized (snapshot) so the audit is meaningful after session deletion.

CREATE TABLE IF NOT EXISTS agent_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    agent_name   TEXT    NOT NULL,
    session_uuid TEXT    CHECK (session_uuid IS NULL OR length(session_uuid) = 36),
    session_name TEXT,
    action       TEXT    NOT NULL,
    payload      TEXT,
    result       TEXT    NOT NULL DEFAULT 'ok',
    detail       TEXT
);

CREATE INDEX IF NOT EXISTS idx_agent_actions_session
    ON agent_actions(session_uuid);

CREATE INDEX IF NOT EXISTS idx_agent_actions_agent
    ON agent_actions(agent_name, created_at);
