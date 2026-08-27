-- Migration 170 — Notifications per-user rebuild: extend target_type CHECK + rollup_count
--
-- P1 collaboration (docs/plans/2026-07-03-feat-notifications-comments-plan.md, F1).
-- Two schema changes that SQLite cannot do via ALTER TABLE (CHECK is immutable),
-- so this is the standard table-rebuild (mirror of 077/091):
--
--   A. target_type CHECK extended: ('task','pr') -> +('project','finding','drift',
--      'user_provisioning_request'). The last one LEGALIZES an insert that already
--      exists today: user_provisioning.py:_notify_admins_poison inserts
--      target_type='user_provisioning_request', which violates the current CHECK and
--      dies silently in its except (the poison notification never lands). F1 also
--      migrates that writer onto notify(); this migration makes the row legal.
--
--   B. New column rollup_count INTEGER NOT NULL DEFAULT 1 — the anti-spam counter.
--      notify() bumps it on an existing UNREAD row for the same (user, type, target)
--      instead of inserting a duplicate. Existing rows backfill to 1.
--
-- Recreates the 6 live indices (verified against the tenant DB, NOT just mig 034:
-- workspace_id + pushed_at were added by later migrations) and adds one new partial
-- index for the notices GROUP BY (F4): idx_notifications_unread_scope.
--
-- Nothing references INTO notifications (verified: no FK targets this table), so the
-- rebuild is a straight copy. FK OUT (user_id->users, event_id->events) preserved.
--
-- Reversibile: migrations/170_notifications_rollup_down.sql (restore dallo stato live, non dallo snapshot).

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

-- Backup safety net (data-integrity C4, mirror 077).
DROP TABLE IF EXISTS notifications_backup_170;
CREATE TABLE notifications_backup_170 AS SELECT * FROM notifications;

-- Rebuilt table: extended CHECK + rollup_count. Column order/defaults mirror the
-- LIVE schema (id..created_at from mig 034, pushed_at + workspace_id from later mig).
CREATE TABLE notifications_new (
    id          TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    user_id     TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    event_id    TEXT REFERENCES events(id) ON DELETE SET NULL,
    type        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT,
    target_type TEXT CHECK(target_type IN (
        'task', 'pr', 'project', 'finding', 'drift', 'user_provisioning_request'
    )),
    target_id   TEXT,
    project     TEXT,
    read_at     TEXT DEFAULT NULL,
    acted_at    TEXT DEFAULT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now', 'utc')),
    pushed_at   TEXT,
    workspace_id TEXT,
    rollup_count INTEGER NOT NULL DEFAULT 1
);

-- Explicit columns (data-integrity C1, NOT SELECT *). rollup_count backfills to 1.
INSERT INTO notifications_new (
    id, user_id, event_id, type, title, body, target_type, target_id, project,
    read_at, acted_at, created_at, pushed_at, workspace_id, rollup_count
)
SELECT
    id, user_id, event_id, type, title, body, target_type, target_id, project,
    read_at, acted_at, created_at, pushed_at, workspace_id, 1
FROM notifications;

DROP TABLE notifications;
ALTER TABLE notifications_new RENAME TO notifications;

-- Recreate the 6 live indices (verified against tenant DB) --------------------
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

-- New: the notices GROUP BY (F4) — index-only COUNT of unread by (user, project, type).
CREATE INDEX IF NOT EXISTS idx_notifications_unread_scope
    ON notifications (user_id, project, type)
    WHERE read_at IS NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (170);

COMMIT;

PRAGMA foreign_keys=ON;
