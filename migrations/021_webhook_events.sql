-- v1.0.0 - 2026-03-01 - Webhook events audit log
CREATE TABLE IF NOT EXISTS webhook_events (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    delivery_id TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    action      TEXT,
    repo        TEXT,
    pr_number   INTEGER,
    branch      TEXT,
    task_id     TEXT,
    pr_id       TEXT,
    payload     TEXT,
    processed   INTEGER NOT NULL DEFAULT 0,
    error       TEXT,
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_task_id ON webhook_events(task_id);
CREATE INDEX IF NOT EXISTS idx_webhook_events_created_at ON webhook_events(created_at);
