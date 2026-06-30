-- v1.0.0 - 2026-02-25 - Projects module: status updates, comments, reactions
-- NOTE: project slugs in these tables reference programs.yaml / .task files.
-- If a project is renamed, update manually:
-- UPDATE project_status_updates SET project = 'new-slug' WHERE project = 'old-slug';
-- UPDATE comments SET target_id = 'new-slug' WHERE target_type = 'project' AND target_id = 'old-slug';

-- Status updates for projects
CREATE TABLE IF NOT EXISTS project_status_updates (
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
CREATE INDEX IF NOT EXISTS idx_status_updates_project
    ON project_status_updates(project);
CREATE INDEX IF NOT EXISTS idx_status_updates_project_created
    ON project_status_updates(project, created_at DESC);

-- Unified comments (program, project, task)
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type TEXT NOT NULL CHECK(target_type IN ('program','project','task')),
    target_id TEXT NOT NULL,
    body TEXT NOT NULL CHECK(length(trim(body)) > 0),
    status TEXT NOT NULL DEFAULT 'info' CHECK(status IN ('info','question','blocker','resolved')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    edited_at TEXT,
    parent_id INTEGER REFERENCES comments(id) ON DELETE RESTRICT,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_comments_target
    ON comments(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_comments_parent
    ON comments(parent_id);
CREATE INDEX IF NOT EXISTS idx_comments_created
    ON comments(created_at DESC);

-- Prevent replies deeper than 1 level
CREATE TRIGGER IF NOT EXISTS trg_comments_max_depth
BEFORE INSERT ON comments
WHEN NEW.parent_id IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'Cannot reply to a reply (max depth 1)')
    WHERE EXISTS (SELECT 1 FROM comments WHERE id = NEW.parent_id AND parent_id IS NOT NULL);
END;

-- Comment reactions
CREATE TABLE IF NOT EXISTS comment_reactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    comment_id INTEGER NOT NULL REFERENCES comments(id) ON DELETE RESTRICT,
    reaction TEXT NOT NULL CHECK(reaction IN ('+1','-1','eyes','check')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(comment_id, reaction, created_by)
);
CREATE INDEX IF NOT EXISTS idx_reactions_comment
    ON comment_reactions(comment_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (4);
