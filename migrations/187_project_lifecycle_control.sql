-- Migration 187: first-class project lifecycle, write fencing, and Cloud/F lease.
--
-- Every database-backed project mutation emits one project_write_events row.
-- The event table owns the single writability check: aborting its INSERT also
-- rolls back the outer statement that fired it.  Filesystem writers call the
-- same journal explicitly while holding the shared project mutation lock.

BEGIN IMMEDIATE;

CREATE TABLE project_lifecycle_state (
    workspace_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    project_id TEXT NOT NULL UNIQUE,
    lifecycle TEXT NOT NULL DEFAULT 'active' CHECK (
        lifecycle IN ('idea','planning','active','maintenance','archived')
    ),
    project_digest TEXT,
    writer_watermark INTEGER NOT NULL DEFAULT 0 CHECK (writer_watermark >= 0),
    selector_watermark TEXT NOT NULL DEFAULT '',
    transition_operation_id TEXT,
    archived_at TEXT,
    archived_by TEXT,
    archive_approval_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, project_slug),
    CHECK (
        (lifecycle = 'archived' AND archived_at IS NOT NULL)
        OR (lifecycle != 'archived' AND archived_at IS NULL)
    )
);

CREATE INDEX idx_project_lifecycle_selectable
    ON project_lifecycle_state(workspace_id, lifecycle, project_slug);

-- The SQL migration cannot inspect project.yaml.  The synchronous v187
-- post-hook seeds every existing filesystem project (including legacy archived
-- projects) before startup can continue, then seals this marker.  A claimed
-- v187 with a pending marker is a repair that requires writer quiescence.
CREATE TABLE project_lifecycle_bootstrap (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    state TEXT NOT NULL CHECK (state IN ('pending','complete')),
    project_count INTEGER NOT NULL DEFAULT 0 CHECK (project_count >= 0),
    archived_count INTEGER NOT NULL DEFAULT 0 CHECK (archived_count >= 0),
    snapshot_digest TEXT,
    completed_at TEXT,
    CHECK (
        (state = 'pending' AND snapshot_digest IS NULL AND completed_at IS NULL)
        OR (state = 'complete' AND snapshot_digest IS NOT NULL AND completed_at IS NOT NULL)
    )
);

INSERT INTO project_lifecycle_bootstrap(id,state) VALUES (1,'pending');

CREATE TABLE project_write_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    writer_kind TEXT NOT NULL,
    operation_id TEXT,
    actor TEXT,
    resource_ref TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (workspace_id, project_slug)
        REFERENCES project_lifecycle_state(workspace_id, project_slug)
        ON DELETE RESTRICT
);

CREATE INDEX idx_project_write_events_watermark
    ON project_write_events(workspace_id, project_slug, id);

CREATE TRIGGER project_write_events_writability_gate
BEFORE INSERT ON project_write_events
FOR EACH ROW
WHEN EXISTS (
    SELECT 1
      FROM project_lifecycle_state state
     WHERE state.workspace_id = NEW.workspace_id
       AND state.project_slug = NEW.project_slug
       AND (
           state.lifecycle = 'archived'
           OR state.transition_operation_id IS NOT NULL
       )
)
BEGIN
    SELECT RAISE(ABORT, 'project_not_writable');
END;

CREATE TRIGGER project_write_events_advance_watermark
AFTER INSERT ON project_write_events
FOR EACH ROW
BEGIN
    UPDATE project_lifecycle_state
       SET writer_watermark = writer_watermark + 1,
           updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE workspace_id = NEW.workspace_id
       AND project_slug = NEW.project_slug;
END;

CREATE TRIGGER project_write_events_append_only_update
BEFORE UPDATE ON project_write_events
BEGIN
    SELECT RAISE(ABORT, 'project_write_events_append_only');
END;

CREATE TRIGGER project_write_events_append_only_delete
BEFORE DELETE ON project_write_events
BEGIN
    SELECT RAISE(ABORT, 'project_write_events_append_only');
END;

CREATE TABLE cloud_f_control (
    workspace_id TEXT PRIMARY KEY,
    change_epoch INTEGER NOT NULL DEFAULT 0 CHECK (change_epoch >= 0),
    readiness_state TEXT NOT NULL DEFAULT 'bootstrap_required' CHECK (
        readiness_state IN ('bootstrap_required','ready')
    ),
    readiness_subtype TEXT CHECK (
        readiness_subtype IS NULL OR readiness_subtype IN (
            'bootstrap_activation','existing_live_adoption'
        )
    ),
    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
    lease_operation_id TEXT,
    lease_owner TEXT,
    lease_expires_at TEXT,
    activated_at TEXT,
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    CHECK (
        (lease_operation_id IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL)
        OR (lease_operation_id IS NOT NULL AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (readiness_state = 'bootstrap_required' AND readiness_subtype IS NULL)
        OR (readiness_state = 'ready' AND readiness_subtype IS NOT NULL AND activated_at IS NOT NULL)
    )
);

CREATE TABLE cloud_f_change_operations (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    base_epoch INTEGER NOT NULL CHECK (base_epoch >= 0),
    lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
    state TEXT NOT NULL CHECK (state IN ('active','completed')),
    advance_epoch INTEGER CHECK (advance_epoch IN (0,1)),
    result_epoch INTEGER CHECK (result_epoch IS NULL OR result_epoch >= 0),
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    PRIMARY KEY (workspace_id, operation_id),
    FOREIGN KEY (workspace_id) REFERENCES cloud_f_control(workspace_id)
        ON DELETE RESTRICT,
    CHECK (
        (state = 'active' AND advance_epoch IS NULL AND result_epoch IS NULL
            AND result_json IS NULL AND completed_at IS NULL)
        OR (state = 'completed' AND advance_epoch IS NOT NULL
            AND result_epoch IS NOT NULL AND result_json IS NOT NULL
            AND completed_at IS NOT NULL)
    )
);

CREATE TABLE cloud_f_active_operations (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    operation_kind TEXT NOT NULL,
    actor TEXT NOT NULL,
    base_epoch INTEGER NOT NULL CHECK (base_epoch >= 0),
    lease_generation INTEGER NOT NULL CHECK (lease_generation >= 1),
    started_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, operation_id),
    FOREIGN KEY (workspace_id) REFERENCES cloud_f_control(workspace_id)
        ON DELETE RESTRICT
);

CREATE TABLE project_archive_approvals (
    approval_id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    expected_lifecycle TEXT NOT NULL,
    expected_project_digest TEXT NOT NULL,
    plan_f_digest TEXT NOT NULL,
    master_digest TEXT NOT NULL,
    evidence_digest TEXT NOT NULL,
    expected_writer_watermark INTEGER NOT NULL CHECK (expected_writer_watermark >= 0),
    expected_selector_watermark TEXT NOT NULL,
    expected_cloud_f_epoch INTEGER NOT NULL CHECK (expected_cloud_f_epoch >= 0),
    expected_active_operations_digest TEXT NOT NULL,
    approved_by TEXT NOT NULL,
    authority_kind TEXT NOT NULL,
    authority_grant_id TEXT,
    approved_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    expires_at TEXT NOT NULL,
    consumed_by_operation_id TEXT,
    consumed_at TEXT,
    FOREIGN KEY (workspace_id, project_slug)
        REFERENCES project_lifecycle_state(workspace_id, project_slug)
        ON DELETE RESTRICT,
    CHECK (
        (consumed_by_operation_id IS NULL AND consumed_at IS NULL)
        OR (consumed_by_operation_id IS NOT NULL AND consumed_at IS NOT NULL)
    )
);

CREATE INDEX idx_project_archive_approvals_project
    ON project_archive_approvals(workspace_id, project_slug, expires_at);

CREATE TABLE project_lifecycle_operations (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (operation_kind IN ('archive')),
    actor TEXT NOT NULL,
    project_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    approval_id TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared','filesystem_applied','completed','failed')
    ),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    PRIMARY KEY (workspace_id, operation_id),
    UNIQUE (workspace_id, idempotency_key),
    FOREIGN KEY (approval_id) REFERENCES project_archive_approvals(approval_id)
        ON DELETE RESTRICT
);

CREATE INDEX idx_project_lifecycle_operations_project
    ON project_lifecycle_operations(workspace_id, project_slug, created_at);

CREATE TABLE governed_decisions (
    workspace_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    relative_path TEXT NOT NULL,
    content_digest TEXT NOT NULL,
    body_digest TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (
        lifecycle IN ('draft','accepted','superseded')
    ),
    created_by TEXT NOT NULL,
    accepted_by TEXT,
    accepted_at TEXT,
    superseded_by_decision_id TEXT,
    superseded_by_project_slug TEXT,
    superseded_by_path TEXT,
    superseded_at TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, decision_id),
    UNIQUE (workspace_id, project_slug, relative_path),
    FOREIGN KEY (workspace_id, project_slug)
        REFERENCES project_lifecycle_state(workspace_id, project_slug)
        ON DELETE RESTRICT,
    CHECK (
        (lifecycle = 'accepted' AND accepted_by IS NOT NULL AND accepted_at IS NOT NULL)
        OR lifecycle != 'accepted'
    ),
    CHECK (
        (lifecycle = 'superseded'
            AND superseded_by_decision_id IS NOT NULL
            AND superseded_by_project_slug IS NOT NULL
            AND superseded_by_path IS NOT NULL
            AND superseded_at IS NOT NULL)
        OR lifecycle != 'superseded'
    )
);

CREATE TABLE decision_lifecycle_operations (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation_kind TEXT NOT NULL CHECK (
        operation_kind IN ('create','accept','supersede','pointer')
    ),
    primary_project_slug TEXT NOT NULL,
    actor TEXT NOT NULL,
    cloud_f_epoch INTEGER NOT NULL CHECK (cloud_f_epoch >= 0),
    request_json TEXT NOT NULL,
    request_digest TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('prepared','filesystem_applied','completed','failed')
    ),
    result_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    completed_at TEXT,
    PRIMARY KEY (workspace_id, operation_id),
    UNIQUE (workspace_id, idempotency_key)
);

CREATE TABLE historical_artifact_pointers (
    workspace_id TEXT NOT NULL,
    operation_id TEXT NOT NULL,
    source_project_slug TEXT NOT NULL,
    source_kind TEXT NOT NULL CHECK (source_kind IN ('decision','handoff','learning')),
    source_relative_path TEXT NOT NULL,
    source_body_digest TEXT NOT NULL,
    relation TEXT NOT NULL CHECK (relation IN ('forward','applies_to')),
    target_project_slug TEXT NOT NULL,
    target_decision_id TEXT,
    target_relative_path TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (workspace_id, operation_id),
    UNIQUE (
        workspace_id, source_project_slug, source_kind, source_relative_path,
        relation, target_project_slug, target_decision_id, target_relative_path
    ),
    FOREIGN KEY (workspace_id, source_project_slug)
        REFERENCES project_lifecycle_state(workspace_id, project_slug)
        ON DELETE RESTRICT
);

CREATE UNIQUE INDEX idx_historical_artifact_pointer_identity
    ON historical_artifact_pointers(
        workspace_id, source_project_slug, source_kind, source_relative_path,
        relation, target_project_slug,
        COALESCE(target_decision_id,''), COALESCE(target_relative_path,'')
    );

-- Seed lifecycle state lazily from every database-backed writer.  The random
-- project_id is generated only once by the PRIMARY KEY conflict guard.

CREATE TRIGGER project_writes_tasks_insert
AFTER INSERT ON tasks
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'task',NEW.created_by,NEW.id);
END;

CREATE TRIGGER project_writes_tasks_update
AFTER UPDATE ON tasks
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'task',NEW.created_by,NEW.id);
END;

CREATE TRIGGER project_writes_tasks_delete
AFTER DELETE ON tasks
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'task',OLD.created_by,OLD.id);
END;

-- A PR is project work just like its parent task.  Resolve legacy NULL
-- workspace rows from the task first, then the explicit PR coordinate, then a
-- unique project owner.  This closes webhook/service paths that otherwise
-- remained writable after archive even though task mutations were fenced.
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
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
            NULLIF(trim(NEW.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=NEW.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        NEW.project,'prj_' || lower(hex(randomblob(16)))
    );
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
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=NEW.task_id),
            NULLIF(trim(NEW.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=NEW.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        NEW.project,'prj_' || lower(hex(randomblob(16)))
    );
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
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
            NULLIF(trim(OLD.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=OLD.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        OLD.project,'prj_' || lower(hex(randomblob(16)))
    );
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

CREATE TRIGGER project_writes_learnings_insert
AFTER INSERT ON learnings
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'learning',NEW.id);
END;

CREATE TRIGGER project_writes_learnings_update
AFTER UPDATE ON learnings
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'learning',NEW.id);
END;

CREATE TRIGGER project_writes_learnings_delete
AFTER DELETE ON learnings
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'learning',OLD.id);
END;

CREATE TRIGGER project_writes_todos_insert
AFTER INSERT ON todos
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project,'todo',NEW.id);
END;

CREATE TRIGGER project_writes_todos_update
AFTER UPDATE ON todos
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project,'todo',NEW.id);
END;

CREATE TRIGGER project_writes_todos_delete
AFTER DELETE ON todos
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project,'todo',OLD.id);
END;

CREATE TRIGGER project_writes_status_insert
AFTER INSERT ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'prj_' || lower(hex(randomblob(16)))
    );
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'status',NEW.created_by,CAST(NEW.id AS TEXT)
    );
END;

CREATE TRIGGER project_writes_status_update
AFTER UPDATE ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.project) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'prj_' || lower(hex(randomblob(16)))
    );
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=NEW.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        NEW.project,'status',NEW.created_by,CAST(NEW.id AS TEXT)
    );
END;

CREATE TRIGGER project_writes_status_delete
AFTER DELETE ON project_status_updates
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'prj_' || lower(hex(randomblob(16)))
    );
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'status',OLD.created_by,CAST(OLD.id AS TEXT)
    );
END;

CREATE TRIGGER project_writes_file_meta_insert
AFTER INSERT ON file_meta
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'file_meta',NEW.owner_user_id,NEW.rel_path);
END;

CREATE TRIGGER project_writes_file_meta_update
AFTER UPDATE ON file_meta
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'file_meta',NEW.owner_user_id,NEW.rel_path);
END;

CREATE TRIGGER project_writes_file_meta_delete
AFTER DELETE ON file_meta
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'file_meta',OLD.owner_user_id,OLD.rel_path);
END;

CREATE TRIGGER project_writes_documents_insert
AFTER INSERT ON documents
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'document_index',NEW.file_path);
END;

CREATE TRIGGER project_writes_documents_update
AFTER UPDATE ON documents
WHEN NEW.project IS NOT NULL AND length(trim(NEW.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project,'document_index',NEW.file_path);
END;

CREATE TRIGGER project_writes_documents_delete
AFTER DELETE ON documents
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,'document_index',OLD.file_path);
END;

CREATE TRIGGER project_writes_ingest_pending_insert
AFTER INSERT ON ingest_pending
WHEN NEW.project_slug IS NOT NULL AND length(trim(NEW.project_slug)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,'ingest',NEW.id);
END;

CREATE TRIGGER project_writes_ingest_pending_update
AFTER UPDATE ON ingest_pending
WHEN NEW.project_slug IS NOT NULL AND length(trim(NEW.project_slug)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(NEW.workspace_id,'ws_default'),NEW.project_slug,'ingest',NEW.id);
END;

CREATE TRIGGER project_writes_ingest_pending_delete
AFTER DELETE ON ingest_pending
WHEN OLD.project_slug IS NOT NULL AND length(trim(OLD.project_slug)) > 0
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,'ingest',OLD.id);
END;

CREATE TRIGGER project_writes_gui_metadata_insert
AFTER INSERT ON project_gui_metadata
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_metadata','gui');
END;

CREATE TRIGGER project_writes_gui_metadata_update
AFTER UPDATE ON project_gui_metadata
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_metadata','gui');
END;

CREATE TRIGGER project_writes_gui_metadata_delete
AFTER DELETE ON project_gui_metadata
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_metadata','gui');
END;

CREATE TRIGGER project_writes_workspace_projects_insert
AFTER INSERT ON workspace_projects
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_ownership',NEW.created_by,NEW.source);
END;

CREATE TRIGGER project_writes_workspace_projects_update
AFTER UPDATE ON workspace_projects
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (NEW.workspace_id,NEW.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (NEW.workspace_id,NEW.project_slug,'project_ownership',NEW.created_by,NEW.source);
END;

CREATE TRIGGER project_writes_workspace_projects_delete
AFTER DELETE ON workspace_projects
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_ownership',OLD.created_by,OLD.source);
END;

-- Project and task comments are resolved to their single workspace owner.  A
-- program comment is not project-scoped and therefore emits no lifecycle event.
CREATE TRIGGER project_writes_comments_insert
AFTER INSERT ON comments
WHEN NEW.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE NEW.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.target_id) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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

CREATE TRIGGER project_writes_comments_update
AFTER UPDATE ON comments
WHEN NEW.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE NEW.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=NEW.target_id) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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

CREATE TRIGGER project_writes_comments_delete
AFTER DELETE ON comments
WHEN OLD.target_type IN ('project','task')
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE OLD.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.target_id) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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

-- Reactions mutate the same project-scoped discussion as their parent comment.
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
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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

-- Reactions are toggled by delete/reinsert; no supported writer mutates one in
-- place. Rejecting UPDATE closes an otherwise unjournaled project mutation
-- without inventing ambiguous source/destination semantics for comment moves.
CREATE TRIGGER project_writes_comment_reactions_immutable
BEFORE UPDATE ON comment_reactions
BEGIN
    SELECT RAISE(ABORT, 'comment_reactions_immutable');
END;

-- UPDATE triggers above fence the destination scope.  These BEFORE triggers
-- also fence the source scope when a row is moved between projects/workspaces;
-- otherwise an archived project could be mutated by moving its last reference
-- out to an active project.
CREATE TRIGGER project_writes_tasks_update_old_scope
BEFORE UPDATE OF project, workspace_id ON tasks
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'task_move_source',OLD.created_by,OLD.id);
END;

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
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE(
            (SELECT NULLIF(trim(workspace_id),'') FROM tasks WHERE id=OLD.task_id),
            NULLIF(trim(OLD.workspace_id),''),
            (SELECT MIN(workspace_id) FROM workspace_projects
              WHERE project_slug=OLD.project
              HAVING COUNT(DISTINCT workspace_id)=1),
            'ws_default'
        ),
        OLD.project,'prj_' || lower(hex(randomblob(16)))
    );
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

CREATE TRIGGER project_writes_learnings_update_old_scope
BEFORE UPDATE OF project, workspace_id ON learnings
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'learning_move_source',OLD.id);
END;

CREATE TRIGGER project_writes_todos_update_old_scope
BEFORE UPDATE OF project, workspace_id ON todos
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project OR OLD.workspace_id IS NOT NEW.workspace_id)
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project,'todo_move_source',OLD.id);
END;

CREATE TRIGGER project_writes_status_update_old_scope
BEFORE UPDATE OF project ON project_status_updates
WHEN OLD.project IS NOT NEW.project
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.project) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'prj_' || lower(hex(randomblob(16)))
    );
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (
        COALESCE((SELECT MIN(workspace_id) FROM workspace_projects
                  WHERE project_slug=OLD.project
                  HAVING COUNT(DISTINCT workspace_id)=1),'ws_default'),
        OLD.project,'status_move_source',OLD.created_by,CAST(OLD.id AS TEXT)
    );
END;

CREATE TRIGGER project_writes_file_meta_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON file_meta
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'file_meta_move_source',
            OLD.owner_user_id,OLD.rel_path);
END;

CREATE TRIGGER project_writes_documents_update_old_scope
BEFORE UPDATE OF project, workspace_id ON documents
WHEN OLD.project IS NOT NULL AND length(trim(OLD.project)) > 0
 AND (OLD.project IS NOT NEW.project
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project,
            'document_move_source',OLD.file_path);
END;

CREATE TRIGGER project_writes_ingest_pending_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON ingest_pending
WHEN OLD.project_slug IS NOT NULL AND length(trim(OLD.project_slug)) > 0
 AND (OLD.project_slug IS NOT NEW.project_slug
      OR COALESCE(OLD.workspace_id,'ws_default')
         <> COALESCE(NEW.workspace_id,'ws_default'))
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,
            'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (COALESCE(OLD.workspace_id,'ws_default'),OLD.project_slug,
            'ingest_move_source',OLD.id);
END;

CREATE TRIGGER project_writes_gui_metadata_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON project_gui_metadata
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_metadata_move_source','gui');
END;

CREATE TRIGGER project_writes_workspace_projects_update_old_scope
BEFORE UPDATE OF project_slug, workspace_id ON workspace_projects
WHEN OLD.project_slug IS NOT NEW.project_slug
  OR OLD.workspace_id IS NOT NEW.workspace_id
BEGIN
    INSERT OR IGNORE INTO project_lifecycle_state
        (workspace_id,project_slug,project_id)
    VALUES (OLD.workspace_id,OLD.project_slug,'prj_' || lower(hex(randomblob(16))));
    INSERT INTO project_write_events
        (workspace_id,project_slug,writer_kind,actor,resource_ref)
    VALUES (OLD.workspace_id,OLD.project_slug,'project_ownership_move_source',
            OLD.created_by,OLD.source);
END;

CREATE TRIGGER project_writes_comments_update_old_scope
BEFORE UPDATE OF target_type, target_id ON comments
WHEN OLD.target_type IN ('project','task')
 AND (OLD.target_type IS NOT NEW.target_type OR OLD.target_id IS NOT NEW.target_id)
BEGIN
    SELECT RAISE(ABORT, 'project_workspace_ambiguous')
     WHERE OLD.target_type='project'
       AND (SELECT COUNT(DISTINCT workspace_id) FROM workspace_projects
             WHERE project_slug=OLD.target_id) > 1;
    INSERT OR IGNORE INTO project_lifecycle_state
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
      ) scope;
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

INSERT OR IGNORE INTO schema_versions(version) VALUES (187);

COMMIT;
