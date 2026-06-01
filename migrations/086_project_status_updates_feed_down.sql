-- v1.0.0 - 2026-04-22 - Rollback feed-style columns added in 086
-- SQLite doesn't support DROP COLUMN in older versions reliably; we rebuild
-- the table via a shadow copy, preserving the original structured columns.

CREATE TABLE project_status_updates_old AS
    SELECT id, project, status, what_done, blockers, next_steps,
           created_by, created_at, updated_at
    FROM project_status_updates;

DROP TABLE project_status_updates;

CREATE TABLE project_status_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('active','paused','blocked','completed','not_started')),
    what_done TEXT,
    blockers TEXT,
    next_steps TEXT,
    created_by TEXT NOT NULL DEFAULT 'console',
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT
);

INSERT INTO project_status_updates
    (id, project, status, what_done, blockers, next_steps, created_by, created_at, updated_at)
    SELECT id, project, status, what_done, blockers, next_steps, created_by, created_at, updated_at
    FROM project_status_updates_old;

DROP TABLE project_status_updates_old;

CREATE INDEX IF NOT EXISTS idx_status_updates_project
    ON project_status_updates(project);
CREATE INDEX IF NOT EXISTS idx_status_updates_project_created
    ON project_status_updates(project, created_at DESC);

DELETE FROM schema_versions WHERE version = 86;
