-- v052 - 2026-04-07 - Add Task.kind for idea vs normal triage
ALTER TABLE tasks ADD COLUMN kind TEXT NOT NULL DEFAULT 'normal' CHECK (kind IN ('normal', 'idea'));
UPDATE tasks SET kind = 'normal' WHERE kind IS NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_kind ON tasks(kind) WHERE deleted_at IS NULL;
