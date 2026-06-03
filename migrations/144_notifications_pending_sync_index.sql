-- 144: partial index for the notifications acted_at background sync loop.
--
-- _notifications_acted_at_sync_loop (core/api/main.py) runs every 60s:
--   UPDATE notifications SET acted_at=?, read_at=COALESCE(read_at,?)
--   WHERE acted_at IS NULL AND type='task_pending' AND target_type='task'
--   AND target_id NOT IN (SELECT id FROM tasks WHERE status='pending')
--
-- No existing index covers (acted_at IS NULL AND type='task_pending'), so the
-- outer scan was a full table scan -> measured ~15s writer-lock hold on
-- 2026-05-29 (terminal-metrics dump, hold_by_label). This partial index is
-- tiny (only the still-pending notifications) and exactly matches the hot set.
CREATE INDEX IF NOT EXISTS idx_notifications_pending_sync
    ON notifications (type, acted_at)
    WHERE acted_at IS NULL AND type = 'task_pending';
