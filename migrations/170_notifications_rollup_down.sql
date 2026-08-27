-- Down-migration 170 — revert the notifications rollup rebuild, preserving live state.
--
-- Emergency rollback only. Restores the pre-170 structure: no rollup_count, and
-- target_type CHECK back to ('task', 'pr').
--
-- Data semantics: rows are restored from the LIVE notifications table, NOT from the
-- up-time snapshot notifications_backup_170. Restoring from that snapshot would
-- silently discard two classes of row — pre-migration rows edited after the
-- up-migration, whose stale values the snapshot still holds, and every row created
-- after the up-migration, which the snapshot never contained at all.
--
-- The single data loss is the one inherent to reverting the CHECK: rows whose
-- target_type was legalized by 170 ('project', 'finding', 'drift',
-- 'user_provisioning_request') cannot exist under the restored constraint and are
-- dropped. Rows with target_type NULL, 'task' or 'pr' keep their current values.
--
-- Both the up-time snapshot and the staging table are dropped at the end, so a
-- completed rollback leaves no scratch tables behind.

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Stage the rows the pre-170 schema can represent, read from the LIVE table.
DROP TABLE IF EXISTS notifications_restore_170;
CREATE TABLE notifications_restore_170 AS
SELECT
    id, user_id, event_id, type, title, body, target_type, target_id, project,
    read_at, acted_at, created_at, pushed_at, workspace_id
FROM notifications
WHERE target_type IS NULL OR target_type IN ('task', 'pr');

DROP TABLE IF EXISTS notifications;

CREATE TABLE notifications (
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
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    pushed_at   TEXT,
    workspace_id TEXT
);

INSERT INTO notifications (
    id, user_id, event_id, type, title, body, target_type, target_id, project,
    read_at, acted_at, created_at, pushed_at, workspace_id
)
SELECT
    id, user_id, event_id, type, title, body, target_type, target_id, project,
    read_at, acted_at, created_at, pushed_at, workspace_id
FROM notifications_restore_170;

CREATE INDEX IF NOT EXISTS idx_notifications_user_unread
    ON notifications (user_id, created_at DESC)
    WHERE read_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_user_recent
    ON notifications (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_notifications_target
    ON notifications (target_type, target_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_notifications_event_user
    ON notifications (event_id, user_id)
    WHERE event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_notifications_ws
    ON notifications (workspace_id);
CREATE INDEX IF NOT EXISTS idx_notifications_pending_sync
    ON notifications (type, acted_at)
    WHERE acted_at IS NULL AND type = 'task_pending';

DROP TABLE IF EXISTS notifications_restore_170;
DROP TABLE IF EXISTS notifications_backup_170;

DELETE FROM schema_versions WHERE version = 170;

COMMIT;

PRAGMA foreign_keys=ON;
