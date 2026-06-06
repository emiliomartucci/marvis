-- 032_task_reminders.sql
-- v1.0.0 - 2026-03-10 - Add due_date and reminder tracking to tasks

ALTER TABLE tasks ADD COLUMN due_date TEXT DEFAULT NULL;           -- ISO 8601 date (YYYY-MM-DD)
ALTER TABLE tasks ADD COLUMN reminder_sent_at TEXT DEFAULT NULL;  -- ISO 8601 datetime, null = not sent

INSERT OR IGNORE INTO schema_versions (version, applied_at) VALUES (32, datetime('now', 'utc'));
