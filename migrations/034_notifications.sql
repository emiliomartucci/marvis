-- Migration 034: Notifications table for in-app notification center
-- Stores persistent notifications with read/unread state per user.

CREATE TABLE IF NOT EXISTS notifications (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id    TEXT REFERENCES events(id) ON DELETE SET NULL,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    target_type TEXT CHECK(target_type IN ('task', 'pr')),
    target_id   TEXT,
    project     TEXT,
    read_at     TEXT DEFAULT NULL,
    acted_at    TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'utc'))
);

-- Partial index for unread notifications (most common query)
CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;

-- General index for all notifications by user
CREATE INDEX IF NOT EXISTS idx_notifications_user_recent
    ON notifications (user_id, created_at DESC);

-- Target lookup for marking acted_at after approve/reject
CREATE INDEX IF NOT EXISTS idx_notifications_target
    ON notifications (target_type, target_id);

-- Prevent duplicate notifications for same event+user
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_event_user
    ON notifications (event_id, user_id)
    WHERE event_id IS NOT NULL;

INSERT OR IGNORE INTO schema_versions (version, applied_at)
VALUES (34, datetime('now', 'utc'));
