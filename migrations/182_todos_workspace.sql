-- Migration 182: make persisted todos workspace-owned.
--
-- Historical rows are attributed only from exact persisted provenance. Rows
-- without one non-conflicting owner stay NULL and are quarantined: reads are
-- workspace-filtered and the update guard prevents them from becoming active
-- until an operator records evidence and reconciles them explicitly.

BEGIN IMMEDIATE;

ALTER TABLE todos
ADD COLUMN workspace_id TEXT;

WITH todo_workspace_evidence(todo_id, workspace_id) AS (
    SELECT al.resource_id, al.workspace_id
      FROM audit_log al
     WHERE al.resource_type = 'todo'
       AND al.workspace_id IS NOT NULL
       AND length(trim(al.workspace_id)) > 0
    UNION ALL
    SELECT td.id, task.workspace_id
      FROM todos td
      JOIN tasks task ON task.id = td.linked_task_id
     WHERE task.workspace_id IS NOT NULL
       AND length(trim(task.workspace_id)) > 0
    UNION ALL
    SELECT td.id, task.workspace_id
      FROM todos td
      JOIN tasks task
        ON task.source = 'todo' AND task.source_ref = td.id
     WHERE task.workspace_id IS NOT NULL
       AND length(trim(task.workspace_id)) > 0
    UNION ALL
    SELECT td.id, MIN(wp.workspace_id)
      FROM todos td
      JOIN workspace_projects wp ON wp.project_slug = td.project
     WHERE wp.workspace_id IS NOT NULL
       AND length(trim(wp.workspace_id)) > 0
     GROUP BY td.id
    HAVING COUNT(DISTINCT wp.workspace_id) = 1
), resolved(todo_id, workspace_id) AS (
    SELECT todo_id,
           CASE WHEN COUNT(DISTINCT workspace_id) = 1
                THEN MIN(workspace_id) ELSE NULL END
      FROM todo_workspace_evidence
     GROUP BY todo_id
)
UPDATE todos
   SET workspace_id = (
       SELECT resolved.workspace_id
         FROM resolved
        WHERE resolved.todo_id = todos.id
   )
 WHERE workspace_id IS NULL OR length(trim(workspace_id)) = 0;

CREATE TRIGGER todos_workspace_required_insert
BEFORE INSERT ON todos
FOR EACH ROW
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
    SELECT RAISE(ABORT, 'todo workspace_id required');
END;

CREATE TRIGGER todos_workspace_required_update
BEFORE UPDATE ON todos
FOR EACH ROW
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
    SELECT RAISE(ABORT, 'todo workspace_id required');
END;

CREATE TRIGGER todos_workspace_immutable
BEFORE UPDATE OF workspace_id ON todos
FOR EACH ROW
WHEN OLD.workspace_id IS NOT NULL
  AND length(trim(OLD.workspace_id)) > 0
  AND NEW.workspace_id IS NOT NULL
  AND length(trim(NEW.workspace_id)) > 0
  AND OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    SELECT RAISE(ABORT, 'todo workspace_id immutable');
END;

CREATE TRIGGER todos_historical_attribution_guard
BEFORE UPDATE OF workspace_id ON todos
FOR EACH ROW
WHEN (OLD.workspace_id IS NULL OR length(trim(OLD.workspace_id)) = 0)
  AND NEW.workspace_id IS NOT NULL
  AND length(trim(NEW.workspace_id)) > 0
  AND (
      NOT EXISTS (
          SELECT 1 FROM (
              SELECT al.workspace_id AS workspace_id
                FROM audit_log al
               WHERE al.resource_type = 'todo'
                 AND al.resource_id = OLD.id
              UNION ALL
              SELECT task.workspace_id
                FROM tasks task
               WHERE task.id = OLD.linked_task_id
              UNION ALL
              SELECT task.workspace_id
                FROM tasks task
               WHERE task.source = 'todo' AND task.source_ref = OLD.id
              UNION ALL
              SELECT wp.workspace_id
                FROM workspace_projects wp
               WHERE wp.project_slug = OLD.project
                 AND (
                     SELECT COUNT(DISTINCT owner.workspace_id)
                       FROM workspace_projects owner
                      WHERE owner.project_slug = OLD.project
                        AND owner.workspace_id IS NOT NULL
                        AND length(trim(owner.workspace_id)) > 0
                 ) = 1
          ) evidence
          WHERE evidence.workspace_id = NEW.workspace_id
      )
      OR EXISTS (
          SELECT 1 FROM (
              SELECT al.workspace_id AS workspace_id
                FROM audit_log al
               WHERE al.resource_type = 'todo'
                 AND al.resource_id = OLD.id
              UNION ALL
              SELECT task.workspace_id
                FROM tasks task
               WHERE task.id = OLD.linked_task_id
              UNION ALL
              SELECT task.workspace_id
                FROM tasks task
               WHERE task.source = 'todo' AND task.source_ref = OLD.id
              UNION ALL
              SELECT wp.workspace_id
                FROM workspace_projects wp
               WHERE wp.project_slug = OLD.project
                 AND (
                     SELECT COUNT(DISTINCT owner.workspace_id)
                       FROM workspace_projects owner
                      WHERE owner.project_slug = OLD.project
                        AND owner.workspace_id IS NOT NULL
                        AND length(trim(owner.workspace_id)) > 0
                 ) = 1
          ) evidence
          WHERE evidence.workspace_id IS NOT NULL
            AND length(trim(evidence.workspace_id)) > 0
            AND evidence.workspace_id != NEW.workspace_id
      )
  )
BEGIN
    SELECT RAISE(ABORT, 'todo workspace attribution not proven');
END;

DROP INDEX IF EXISTS idx_todos_open;
DROP INDEX IF EXISTS idx_todos_project;
DROP INDEX IF EXISTS idx_todos_dedup;

CREATE INDEX idx_todos_workspace_open
ON todos(workspace_id, status, fu, created_at DESC);

CREATE INDEX idx_todos_workspace_project
ON todos(workspace_id, project, fu, created_at DESC);

CREATE UNIQUE INDEX idx_todos_workspace_source_ref
ON todos(workspace_id, source, source_ref)
WHERE source_ref IS NOT NULL;

INSERT OR IGNORE INTO schema_versions(version) VALUES (182);

COMMIT;
