-- 190 — lifecycle-state triggers must not depend on INSERT OR IGNORE.
--
-- SQLite applies the conflict policy of the outer statement to statements in a
-- trigger body. When an UPSERT (INSERT ... ON CONFLICT DO UPDATE) takes its
-- update branch, the AFTER UPDATE trigger's INSERT OR IGNORE therefore aborts
-- with "UNIQUE constraint failed: project_lifecycle_state..." whenever the
-- project already has lifecycle state, i.e. always after migration 187. This
-- broke mark_confidential on every previously written file and silently
-- failed capture_owner and other upserts on file_meta, documents and friends.
--
-- Each trigger keeps its 187 semantics; only the lifecycle-state seed insert
-- becomes an explicit INSERT ... SELECT guarded by NOT EXISTS and non-null keys,
-- which cannot conflict under any outer policy. Generated mechanically from
-- 187_project_lifecycle_control.sql; the down migration restores those bodies.

BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS project_writes_tasks_insert;
CREATE TRIGGER project_writes_tasks_insert
AFTER INSERT ON tasks
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'task',NEW.created_by,NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_tasks_update;
CREATE TRIGGER project_writes_tasks_update
AFTER UPDATE ON tasks
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'task',NEW.created_by,NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_tasks_delete;
CREATE TRIGGER project_writes_tasks_delete
AFTER DELETE ON tasks
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'task',OLD.created_by,OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_pull_requests_insert;
CREATE TRIGGER project_writes_pull_requests_insert
AFTER INSERT ON pull_requests
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE COALESCE(
               (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
               NULLIF(trim(NEW.workspace_id),'')
           ) IS NULL
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE( (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id), NULLIF(trim(NEW.workspace_id),''), (SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=NEW.project HAVING COUNT(DISTINCT workspace_id)=1), 'ws_default' ) AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
            NULLIF(trim(NEW.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=NEW.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        NEW.project,'pull_request',NEW.id
    );
END;

DROP TRIGGER IF EXISTS project_writes_pull_requests_update;
CREATE TRIGGER project_writes_pull_requests_update
AFTER UPDATE ON pull_requests
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE COALESCE(
               (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
               NULLIF(trim(NEW.workspace_id),'')
           ) IS NULL
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE( (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id), NULLIF(trim(NEW.workspace_id),''), (SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=NEW.project HAVING COUNT(DISTINCT workspace_id)=1), 'ws_default' ) AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
            NULLIF(trim(NEW.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=NEW.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        NEW.project,'pull_request',NEW.id
    );
END;

DROP TRIGGER IF EXISTS project_writes_pull_requests_delete;
CREATE TRIGGER project_writes_pull_requests_delete
AFTER DELETE ON pull_requests
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE COALESCE(
               (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
               NULLIF(trim(OLD.workspace_id),'')
           ) IS NULL
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE( (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id), NULLIF(trim(OLD.workspace_id),''), (SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=OLD.project HAVING COUNT(DISTINCT workspace_id)=1), 'ws_default' ) AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
            NULLIF(trim(OLD.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=OLD.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        OLD.project,'pull_request',OLD.id
    );
END;

DROP TRIGGER IF EXISTS project_writes_learnings_insert;
CREATE TRIGGER project_writes_learnings_insert
AFTER INSERT ON learnings
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'learning',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_learnings_update;
CREATE TRIGGER project_writes_learnings_update
AFTER UPDATE ON learnings
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'learning',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_learnings_delete;
CREATE TRIGGER project_writes_learnings_delete
AFTER DELETE ON learnings
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'learning',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_todos_insert;
CREATE TRIGGER project_writes_todos_insert
AFTER INSERT ON todos
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project,'todo',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_todos_update;
CREATE TRIGGER project_writes_todos_update
AFTER UPDATE ON todos
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project,'todo',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_todos_delete;
CREATE TRIGGER project_writes_todos_delete
AFTER DELETE ON todos
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project,'todo',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_status_insert;
CREATE TRIGGER project_writes_status_insert
AFTER INSERT ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=NEW.project HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'status',NEW.created_by,CAST(NEW.id AS TEXT)
    );
END;

DROP TRIGGER IF EXISTS project_writes_status_update;
CREATE TRIGGER project_writes_status_update
AFTER UPDATE ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=NEW.project HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'status',NEW.created_by,CAST(NEW.id AS TEXT)
    );
END;

DROP TRIGGER IF EXISTS project_writes_status_delete;
CREATE TRIGGER project_writes_status_delete
AFTER DELETE ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=OLD.project HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'status',OLD.created_by,CAST(OLD.id AS TEXT)
    );
END;

DROP TRIGGER IF EXISTS project_writes_file_meta_insert;
CREATE TRIGGER project_writes_file_meta_insert
AFTER INSERT ON file_meta
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'file_meta',NEW.owner_user_id,NEW.rel_path);
END;

DROP TRIGGER IF EXISTS project_writes_file_meta_update;
CREATE TRIGGER project_writes_file_meta_update
AFTER UPDATE ON file_meta
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'file_meta',NEW.owner_user_id,NEW.rel_path);
END;

DROP TRIGGER IF EXISTS project_writes_file_meta_delete;
CREATE TRIGGER project_writes_file_meta_delete
AFTER DELETE ON file_meta
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'file_meta',OLD.owner_user_id,OLD.rel_path);
END;

DROP TRIGGER IF EXISTS project_writes_documents_insert;
CREATE TRIGGER project_writes_documents_insert
AFTER INSERT ON documents
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'document_index',NEW.file_path);
END;

DROP TRIGGER IF EXISTS project_writes_documents_update;
CREATE TRIGGER project_writes_documents_update
AFTER UPDATE ON documents
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'document_index',NEW.file_path);
END;

DROP TRIGGER IF EXISTS project_writes_documents_delete;
CREATE TRIGGER project_writes_documents_delete
AFTER DELETE ON documents
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'document_index',OLD.file_path);
END;

DROP TRIGGER IF EXISTS project_writes_ingest_pending_insert;
CREATE TRIGGER project_writes_ingest_pending_insert
AFTER INSERT ON ingest_pending
WHEN NEW.project_slug IS NOT NULL AND length(trim(NEW.project_slug)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,'ingest',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_ingest_pending_update;
CREATE TRIGGER project_writes_ingest_pending_update
AFTER UPDATE ON ingest_pending
WHEN NEW.project_slug IS NOT NULL AND length(trim(NEW.project_slug)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(NEW.workspace_id,'ws_default') AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,'ingest',NEW.id);
END;

DROP TRIGGER IF EXISTS project_writes_ingest_pending_delete;
CREATE TRIGGER project_writes_ingest_pending_delete
AFTER DELETE ON ingest_pending
WHEN OLD.project_slug IS NOT NULL AND length(trim(OLD.project_slug)) > 0
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,'ingest',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_gui_metadata_insert;
CREATE TRIGGER project_writes_gui_metadata_insert
AFTER INSERT ON project_gui_metadata
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_metadata','gui');
END;

DROP TRIGGER IF EXISTS project_writes_gui_metadata_update;
CREATE TRIGGER project_writes_gui_metadata_update
AFTER UPDATE ON project_gui_metadata
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_metadata','gui');
END;

DROP TRIGGER IF EXISTS project_writes_gui_metadata_delete;
CREATE TRIGGER project_writes_gui_metadata_delete
AFTER DELETE ON project_gui_metadata
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_metadata','gui');
END;

DROP TRIGGER IF EXISTS project_writes_workspace_projects_insert;
CREATE TRIGGER project_writes_workspace_projects_insert
AFTER INSERT ON workspace_projects
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_ownership',NEW.created_by,NEW.source);
END;

DROP TRIGGER IF EXISTS project_writes_workspace_projects_update;
CREATE TRIGGER project_writes_workspace_projects_update
AFTER UPDATE ON workspace_projects
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT NEW.workspace_id AS workspace_id,
                   NEW.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_ownership',NEW.created_by,NEW.source);
END;

DROP TRIGGER IF EXISTS project_writes_workspace_projects_delete;
CREATE TRIGGER project_writes_workspace_projects_delete
AFTER DELETE ON workspace_projects
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_ownership',OLD.created_by,OLD.source);
END;

DROP TRIGGER IF EXISTS project_writes_comments_insert;
CREATE TRIGGER project_writes_comments_insert
AFTER INSERT ON comments
WHEN NEW.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE NEW.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.target_id) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=NEW.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               NEW.target_id AS project_slug
         WHERE NEW.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE NEW.target_type='task' AND task.id=NEW.target_id
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment',NEW.created_by,CAST(NEW.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=NEW.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               NEW.target_id AS project_slug
         WHERE NEW.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE NEW.target_type='task' AND task.id=NEW.target_id
      ) scope;
END;

DROP TRIGGER IF EXISTS project_writes_comments_update;
CREATE TRIGGER project_writes_comments_update
AFTER UPDATE ON comments
WHEN NEW.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE NEW.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.target_id) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=NEW.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               NEW.target_id AS project_slug
         WHERE NEW.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE NEW.target_type='task' AND task.id=NEW.target_id
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment',NEW.created_by,CAST(NEW.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=NEW.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               NEW.target_id AS project_slug
         WHERE NEW.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE NEW.target_type='task' AND task.id=NEW.target_id
      ) scope;
END;

DROP TRIGGER IF EXISTS project_writes_comments_delete;
CREATE TRIGGER project_writes_comments_delete
AFTER DELETE ON comments
WHEN OLD.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE OLD.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.target_id) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=OLD.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               OLD.target_id AS project_slug
         WHERE OLD.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE OLD.target_type='task' AND task.id=OLD.target_id
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment',OLD.created_by,CAST(OLD.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=OLD.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               OLD.target_id AS project_slug
         WHERE OLD.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE OLD.target_type='task' AND task.id=OLD.target_id
      ) scope;
END;

DROP TRIGGER IF EXISTS project_writes_comment_reactions_insert;
CREATE TRIGGER project_writes_comment_reactions_insert
AFTER INSERT ON comment_reactions
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE EXISTS (
        SELECT 1 FROM comments comment
         WHERE comment.id=NEW.comment_id
           AND comment.target_type='project'
           AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
                 WHERE project_slug=comment.target_id) > 1
     );
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=comment.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               comment.target_id AS project_slug
          FROM comments comment
         WHERE comment.id=NEW.comment_id AND comment.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM comments comment
          JOIN tasks task ON task.id=comment.target_id
         WHERE comment.id=NEW.comment_id AND comment.target_type='task'
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment_reaction',NEW.created_by,
           CAST(NEW.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=comment.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               comment.target_id AS project_slug
          FROM comments comment
         WHERE comment.id=NEW.comment_id AND comment.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM comments comment
          JOIN tasks task ON task.id=comment.target_id
         WHERE comment.id=NEW.comment_id AND comment.target_type='task'
      ) scope;
END;

DROP TRIGGER IF EXISTS project_writes_comment_reactions_delete;
CREATE TRIGGER project_writes_comment_reactions_delete
AFTER DELETE ON comment_reactions
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE EXISTS (
        SELECT 1 FROM comments comment
         WHERE comment.id=OLD.comment_id
           AND comment.target_type='project'
           AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
                 WHERE project_slug=comment.target_id) > 1
     );
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=comment.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               comment.target_id AS project_slug
          FROM comments comment
         WHERE comment.id=OLD.comment_id AND comment.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM comments comment
          JOIN tasks task ON task.id=comment.target_id
         WHERE comment.id=OLD.comment_id AND comment.target_type='task'
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment_reaction',OLD.created_by,
           CAST(OLD.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=comment.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               comment.target_id AS project_slug
          FROM comments comment
         WHERE comment.id=OLD.comment_id AND comment.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM comments comment
          JOIN tasks task ON task.id=comment.target_id
         WHERE comment.id=OLD.comment_id AND comment.target_type='task'
      ) scope;
END;

DROP TRIGGER IF EXISTS project_writes_tasks_update_old_scope;
CREATE TRIGGER project_writes_tasks_update_old_scope
BEFORE UPDATE OF project, workspace_id ON tasks
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'task_move_source',OLD.created_by,OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_pull_requests_update_old_scope;
CREATE TRIGGER project_writes_pull_requests_update_old_scope
BEFORE UPDATE OF project, workspace_id, task_id ON pull_requests
WHEN OLD.project IS NOT NEW.project
  OR COALESCE(
         (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
         NULLIF(trim(OLD.workspace_id),''),
         (SELECT MIN(workspace_id) FROM workspace_projects
           WHERE project_slug=OLD.project
           HAVING COUNT(DISTINCT workspace_id)=1),
         'ws_default'
     ) <> COALESCE(
         (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
         NULLIF(trim(NEW.workspace_id),''),
         (SELECT MIN(workspace_id) FROM workspace_projects
           WHERE project_slug=NEW.project
           HAVING COUNT(DISTINCT workspace_id)=1),
         'ws_default'
     )
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE COALESCE(
               (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
               NULLIF(trim(OLD.workspace_id),'')
           ) IS NULL
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE( (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id), NULLIF(trim(OLD.workspace_id),''), (SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=OLD.project HAVING COUNT(DISTINCT workspace_id)=1), 'ws_default' ) AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
            NULLIF(trim(OLD.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=OLD.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        OLD.project,'pull_request_move_source',OLD.id
    );
END;

DROP TRIGGER IF EXISTS project_writes_learnings_update_old_scope;
CREATE TRIGGER project_writes_learnings_update_old_scope
BEFORE UPDATE OF project, workspace_id ON learnings
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'learning_move_source',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_todos_update_old_scope;
CREATE TRIGGER project_writes_todos_update_old_scope
BEFORE UPDATE OF project, workspace_id ON todos
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project OR OLD.workspace_id IS NOT NEW.workspace_id)
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project,'todo_move_source',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_status_update_old_scope;
CREATE TRIGGER project_writes_status_update_old_scope
BEFORE UPDATE OF project ON project_status_updates
WHEN OLD.project IS NOT NEW.project
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects WHERE project_slug=OLD.project HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'status_move_source',OLD.created_by,CAST(OLD.id AS TEXT)
    );
END;

DROP TRIGGER IF EXISTS project_writes_file_meta_update_old_scope;
CREATE TRIGGER project_writes_file_meta_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON file_meta
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'file_meta_move_source',
            OLD.owner_user_id,OLD.rel_path);
END;

DROP TRIGGER IF EXISTS project_writes_documents_update_old_scope;
CREATE TRIGGER project_writes_documents_update_old_scope
BEFORE UPDATE OF project, workspace_id ON documents
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'document_move_source',OLD.file_path);
END;

DROP TRIGGER IF EXISTS project_writes_ingest_pending_update_old_scope;
CREATE TRIGGER project_writes_ingest_pending_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON ingest_pending
WHEN OLD.project_slug IS NOT NULL AND length(trim(OLD.project_slug)) > 0
 AND (OLD.project_slug IS NOT NEW.project_slug
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT COALESCE(OLD.workspace_id,'ws_default') AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,
            'ingest_move_source',OLD.id);
END;

DROP TRIGGER IF EXISTS project_writes_gui_metadata_update_old_scope;
CREATE TRIGGER project_writes_gui_metadata_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON project_gui_metadata
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_metadata_move_source','gui');
END;

DROP TRIGGER IF EXISTS project_writes_workspace_projects_update_old_scope;
CREATE TRIGGER project_writes_workspace_projects_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON workspace_projects
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT seed.workspace_id,seed.project_slug,seed.project_id
      FROM (SELECT OLD.workspace_id AS workspace_id,
                   OLD.project_slug AS project_slug,
                   'prj_' || lower(hex(randomblob(16))) AS project_id) seed
     WHERE seed.workspace_id IS NOT NULL AND seed.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=seed.workspace_id
                          AND existing.project_slug=seed.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_ownership_move_source',
            OLD.created_by,OLD.source);
END;

DROP TRIGGER IF EXISTS project_writes_comments_update_old_scope;
CREATE TRIGGER project_writes_comments_update_old_scope
BEFORE UPDATE OF target_type, target_id ON comments
WHEN OLD.target_type IN ('project','task')
 AND (OLD.target_type IS NOT NEW.target_type OR OLD.target_id IS NOT NEW.target_id)
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE OLD.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.target_id) > 1;
    INSERT INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    SELECT scope.workspace_id,scope.project_slug,'prj_' || lower(hex(randomblob(16)))
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=OLD.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               OLD.target_id AS project_slug
         WHERE OLD.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE OLD.target_type='task' AND task.id=OLD.target_id
      ) scope
     WHERE scope.workspace_id IS NOT NULL AND scope.project_slug IS NOT NULL
       AND NOT EXISTS (SELECT 1 FROM project_lifecycle_state existing
                        WHERE existing.workspace_id=scope.workspace_id
                          AND existing.project_slug=scope.project_slug);
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    SELECT scope.workspace_id,scope.project_slug,'comment_move_source',
           OLD.created_by,CAST(OLD.id AS TEXT)
      FROM (
        SELECT COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                         WHERE project_slug=OLD.target_id
                         HAVING COUNT(DISTINCT workspace_id)=1),'ws_default') AS workspace_id,
               OLD.target_id AS project_slug
         WHERE OLD.target_type='project'
        UNION ALL
        SELECT COALESCE(task.workspace_id,'ws_default'),task.project
          FROM tasks task
         WHERE OLD.target_type='task' AND task.id=OLD.target_id
      ) scope;
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (190);
COMMIT;
