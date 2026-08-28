-- Down migration 183: remove workspace ownership only when global keys can be
-- restored without collapsing distinct tenant rows.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

-- These v183 triggers are attached to tables that outlive the first parent
-- rebuild. Remove them transactionally before dropping ingest_pending; a
-- failed collapse guard rolls these drops back with the rest of the down.
DROP TRIGGER IF EXISTS ingest_api_keys_pending_workspace_update;
DROP TRIGGER IF EXISTS ingest_skipped_parent_workspace_insert;
DROP TRIGGER IF EXISTS ingest_skipped_parent_workspace_update;
DROP TRIGGER IF EXISTS ingest_change_history_parent_workspace_insert;
DROP TRIGGER IF EXISTS ingest_change_history_parent_workspace_update;

DROP TABLE IF EXISTS temp.migration_183_down_guard;
CREATE TEMP TABLE migration_183_down_guard (
    ok INTEGER NOT NULL CHECK(ok = 1)
);
INSERT INTO migration_183_down_guard(ok)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM ingest_pending
     GROUP BY sha256, project_slug
    HAVING COUNT(*) > 1
) OR EXISTS (
    SELECT 1 FROM ingest_webhook_nonces
     GROUP BY source, nonce
    HAVING COUNT(*) > 1
) THEN 0 ELSE 1 END;
DROP TABLE migration_183_down_guard;

CREATE TABLE ingest_pending_v182_restore (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    project_slug TEXT NOT NULL DEFAULT 'unclassified',
    source_kind TEXT NOT NULL DEFAULT 'file_drop' CHECK(source_kind IN (
        'file_drop', 'manual_upload', 'api_upload', 'terminal_upload',
        'api_ingress', 'webhook_ingress'
    )),
    mime_type TEXT,
    file_size_bytes INTEGER,
    parser_used TEXT,
    extracted_text TEXT,
    structure_json TEXT,
    classification_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
        'queued', 'parser_waiting', 'parsing', 'classified', 'awaiting_triage',
        'approved', 'inserted', 'done', 'parse_error', 'rejected'
    )),
    error_message TEXT,
    triage_decision_id TEXT,
    target_folder TEXT,
    target_filename TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    recovery_attempts INTEGER NOT NULL DEFAULT 0,
    locked_by TEXT,
    locked_at TEXT,
    api_key_id TEXT,
    source TEXT,
    ingest_policy TEXT,
    ingress_metadata TEXT,
    UNIQUE(sha256, project_slug)
);

INSERT INTO ingest_pending_v182_restore (
    id, file_path, sha256, project_slug, source_kind, mime_type,
    file_size_bytes, parser_used, extracted_text, structure_json,
    classification_json, status, error_message, triage_decision_id,
    target_folder, target_filename, created_at, updated_at,
    recovery_attempts, locked_by, locked_at, api_key_id, source,
    ingest_policy, ingress_metadata
)
SELECT
    id, file_path, sha256, project_slug, source_kind, mime_type,
    file_size_bytes, parser_used, extracted_text, structure_json,
    classification_json, status, error_message, triage_decision_id,
    target_folder, target_filename, created_at, updated_at,
    recovery_attempts, locked_by, locked_at, api_key_id, source,
    ingest_policy, ingress_metadata
FROM ingest_pending;

DROP TABLE ingest_pending;
ALTER TABLE ingest_pending_v182_restore RENAME TO ingest_pending;

CREATE INDEX idx_ingest_pending_status ON ingest_pending(status);
CREATE INDEX idx_ingest_pending_project ON ingest_pending(project_slug);
CREATE INDEX idx_ingest_pending_created ON ingest_pending(created_at DESC);
CREATE INDEX idx_ingest_pending_lock ON ingest_pending(locked_at)
    WHERE locked_at IS NOT NULL;
CREATE INDEX idx_ingest_pending_source ON ingest_pending(source);
CREATE INDEX idx_ingest_pending_api_key ON ingest_pending(api_key_id);

CREATE TABLE ingest_skipped_v182_restore (
    id TEXT PRIMARY KEY,
    file_path_attempted TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    sha256 TEXT,
    reason TEXT NOT NULL CHECK(reason IN (
        'dedup_sha256', 'invalid_path', 'mime_not_allowed',
        'parse_error_pre_dispatch'
    )),
    existing_ingest_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT,
    FOREIGN KEY(existing_ingest_id) REFERENCES ingest_pending(id) ON DELETE SET NULL
);

INSERT INTO ingest_skipped_v182_restore (
    id, file_path_attempted, project_slug, sha256, reason,
    existing_ingest_id, error_message, created_at, created_by
)
SELECT
    id, file_path_attempted, project_slug, sha256, reason,
    existing_ingest_id, error_message, created_at, created_by
FROM ingest_skipped;

DROP TABLE ingest_skipped;
ALTER TABLE ingest_skipped_v182_restore RENAME TO ingest_skipped;

CREATE INDEX idx_ingest_skipped_project_created
    ON ingest_skipped(project_slug, created_at DESC);
CREATE INDEX idx_ingest_skipped_sha256
    ON ingest_skipped(sha256, project_slug);

CREATE TABLE ingest_change_history_v182_restore (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_pending_id TEXT NOT NULL,
    field_name TEXT NOT NULL CHECK(field_name IN (
        'project_slug', 'target_folder', 'target_filename'
    )),
    old_value TEXT,
    new_value TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    changed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    source_ip TEXT,
    user_agent TEXT,
    FOREIGN KEY(ingest_pending_id) REFERENCES ingest_pending(id) ON DELETE CASCADE
);

INSERT INTO ingest_change_history_v182_restore (
    id, ingest_pending_id, field_name, old_value, new_value,
    changed_by, changed_at, source_ip, user_agent
)
SELECT
    id, ingest_pending_id, field_name, old_value, new_value,
    changed_by, changed_at, source_ip, user_agent
FROM ingest_change_history;

DROP TABLE ingest_change_history;
ALTER TABLE ingest_change_history_v182_restore RENAME TO ingest_change_history;

CREATE INDEX idx_change_hist_ingest_id
    ON ingest_change_history(ingest_pending_id, changed_at DESC);

CREATE TABLE ingest_webhook_nonces_v182_restore (
    source TEXT NOT NULL,
    nonce TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY(source, nonce)
);

INSERT INTO ingest_webhook_nonces_v182_restore (
    source, nonce, request_sha256, received_at
)
SELECT source, nonce, request_sha256, received_at
FROM ingest_webhook_nonces;

DROP TABLE ingest_webhook_nonces;
ALTER TABLE ingest_webhook_nonces_v182_restore RENAME TO ingest_webhook_nonces;

CREATE INDEX idx_ingest_webhook_nonces_received
    ON ingest_webhook_nonces(received_at DESC);

DELETE FROM schema_versions WHERE version = 183;

COMMIT;
PRAGMA foreign_keys=ON;
