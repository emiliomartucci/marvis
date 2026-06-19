-- v1.0.0 - 2026-04-10 - Newsletter recipients table + sent_in_newsletter tracking

CREATE TABLE IF NOT EXISTS newsletter_recipients (
    id TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_newsletter_recipients_email
    ON newsletter_recipients(workspace_id, email);

INSERT OR IGNORE INTO schema_versions (version) VALUES (60);
