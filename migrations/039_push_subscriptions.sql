-- Migration 039: Web Push notification subscriptions + outbox tracking
-- Feature: Push notifications (task d2f85bf2)

CREATE TABLE IF NOT EXISTS push_subscriptions (
    endpoint TEXT PRIMARY KEY,
    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now', 'utc'))
);

CREATE INDEX IF NOT EXISTS idx_push_subs_user ON push_subscriptions(user_id);

-- Track which notifications have been pushed (outbox pattern)
ALTER TABLE notifications ADD COLUMN pushed_at TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (39);
