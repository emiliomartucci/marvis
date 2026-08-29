-- Migration 187 rollback: never remove lifecycle fencing while a project is
-- archived/in transition or while the common Cloud/F lease has live state.

BEGIN IMMEDIATE;

CREATE TEMP TABLE v187_lifecycle_rollback_gate (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO v187_lifecycle_rollback_gate(ok)
SELECT CASE WHEN
    EXISTS (
        SELECT 1 FROM project_lifecycle_state
         WHERE lifecycle = 'archived' OR transition_operation_id IS NOT NULL
    )
    OR EXISTS (SELECT 1 FROM cloud_f_active_operations)
    OR EXISTS (
        SELECT 1 FROM cloud_f_control
         WHERE readiness_state = 'ready' OR lease_operation_id IS NOT NULL
    )
THEN 0 ELSE 1 END;
DROP TABLE v187_lifecycle_rollback_gate;

DROP TRIGGER IF EXISTS project_writes_comment_reactions_delete;
DROP TRIGGER IF EXISTS project_writes_comment_reactions_insert;
DROP TRIGGER IF EXISTS project_writes_comment_reactions_immutable;
DROP TRIGGER IF EXISTS project_writes_pull_requests_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_comments_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_workspace_projects_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_gui_metadata_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_ingest_pending_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_documents_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_file_meta_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_status_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_todos_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_learnings_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_tasks_update_old_scope;
DROP TRIGGER IF EXISTS project_writes_comments_delete;
DROP TRIGGER IF EXISTS project_writes_comments_update;
DROP TRIGGER IF EXISTS project_writes_comments_insert;
DROP TRIGGER IF EXISTS project_writes_pull_requests_delete;
DROP TRIGGER IF EXISTS project_writes_pull_requests_update;
DROP TRIGGER IF EXISTS project_writes_pull_requests_insert;
DROP TRIGGER IF EXISTS project_writes_workspace_projects_delete;
DROP TRIGGER IF EXISTS project_writes_workspace_projects_update;
DROP TRIGGER IF EXISTS project_writes_workspace_projects_insert;
DROP TRIGGER IF EXISTS project_writes_gui_metadata_delete;
DROP TRIGGER IF EXISTS project_writes_gui_metadata_update;
DROP TRIGGER IF EXISTS project_writes_gui_metadata_insert;
DROP TRIGGER IF EXISTS project_writes_ingest_pending_delete;
DROP TRIGGER IF EXISTS project_writes_ingest_pending_update;
DROP TRIGGER IF EXISTS project_writes_ingest_pending_insert;
DROP TRIGGER IF EXISTS project_writes_documents_delete;
DROP TRIGGER IF EXISTS project_writes_documents_update;
DROP TRIGGER IF EXISTS project_writes_documents_insert;
DROP TRIGGER IF EXISTS project_writes_file_meta_delete;
DROP TRIGGER IF EXISTS project_writes_file_meta_update;
DROP TRIGGER IF EXISTS project_writes_file_meta_insert;
DROP TRIGGER IF EXISTS project_writes_status_delete;
DROP TRIGGER IF EXISTS project_writes_status_update;
DROP TRIGGER IF EXISTS project_writes_status_insert;
DROP TRIGGER IF EXISTS project_writes_todos_delete;
DROP TRIGGER IF EXISTS project_writes_todos_update;
DROP TRIGGER IF EXISTS project_writes_todos_insert;
DROP TRIGGER IF EXISTS project_writes_learnings_delete;
DROP TRIGGER IF EXISTS project_writes_learnings_update;
DROP TRIGGER IF EXISTS project_writes_learnings_insert;
DROP TRIGGER IF EXISTS project_writes_tasks_delete;
DROP TRIGGER IF EXISTS project_writes_tasks_update;
DROP TRIGGER IF EXISTS project_writes_tasks_insert;
DROP TRIGGER IF EXISTS project_write_events_append_only_delete;
DROP TRIGGER IF EXISTS project_write_events_append_only_update;
DROP TRIGGER IF EXISTS project_write_events_advance_watermark;
DROP TRIGGER IF EXISTS project_write_events_writability_gate;

DROP TABLE historical_artifact_pointers;
DROP TABLE decision_lifecycle_operations;
DROP TABLE governed_decisions;
DROP TABLE project_lifecycle_operations;
DROP TABLE project_archive_approvals;
DROP TABLE cloud_f_active_operations;
DROP TABLE cloud_f_change_operations;
DROP TABLE cloud_f_control;
DROP TABLE project_write_events;
DROP TABLE project_lifecycle_bootstrap;
DROP TABLE project_lifecycle_state;

DELETE FROM schema_versions WHERE version = 187;

COMMIT;
