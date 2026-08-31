-- Migration 183: workspace-own the ingest queue, audit rows, and webhook nonces.
--
-- Legacy ownership is backfilled only from authoritative evidence:
--   1. an ingest API key with an exact workspace, or
--   2. a project slug owned by exactly one workspace_projects row.
-- Ambiguous rows retain workspace_id NULL. Required-workspace triggers keep those
-- rows inert while requiring every new write to carry an authenticated workspace.

PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

CREATE TABLE ingest_pending_v183_new (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
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
    ingress_metadata TEXT
);

INSERT INTO ingest_pending_v183_new (
    id, workspace_id, file_path, sha256, project_slug, source_kind, mime_type,
    file_size_bytes, parser_used, extracted_text, structure_json,
    classification_json, status, error_message, triage_decision_id,
    target_folder, target_filename, created_at, updated_at,
    recovery_attempts, locked_by, locked_at, api_key_id, source,
    ingest_policy, ingress_metadata
)
SELECT
    p.id,
    CASE
      WHEN p.api_key_id IS NOT NULL THEN (
        SELECT k.workspace_id FROM ingest_api_keys k
         WHERE k.id = p.api_key_id
           AND k.workspace_id IS NOT NULL
           AND length(trim(k.workspace_id)) > 0
      )
      WHEN (
        SELECT COUNT(DISTINCT wp.workspace_id)
          FROM workspace_projects wp
         WHERE wp.project_slug = p.project_slug
           AND length(trim(wp.workspace_id)) > 0
      ) = 1 THEN (
        SELECT MIN(wp.workspace_id)
          FROM workspace_projects wp
         WHERE wp.project_slug = p.project_slug
           AND length(trim(wp.workspace_id)) > 0
      )
      ELSE NULL
    END,
    p.file_path, p.sha256, p.project_slug, p.source_kind, p.mime_type,
    p.file_size_bytes, p.parser_used, p.extracted_text, p.structure_json,
    p.classification_json, p.status, p.error_message, p.triage_decision_id,
    p.target_folder, p.target_filename, p.created_at, p.updated_at,
    p.recovery_attempts, p.locked_by, p.locked_at, p.api_key_id, p.source,
    p.ingest_policy, p.ingress_metadata
FROM ingest_pending p;

DROP TABLE ingest_pending;
ALTER TABLE ingest_pending_v183_new RENAME TO ingest_pending;

CREATE UNIQUE INDEX idx_ingest_pending_workspace_sha_project
    ON ingest_pending(workspace_id, sha256, project_slug)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_status
    ON ingest_pending(workspace_id, status, created_at DESC)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_project
    ON ingest_pending(workspace_id, project_slug, created_at DESC)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_created
    ON ingest_pending(workspace_id, created_at DESC)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_lock
    ON ingest_pending(workspace_id, locked_at)
    WHERE workspace_id IS NOT NULL AND locked_at IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_source
    ON ingest_pending(workspace_id, source)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_pending_workspace_api_key
    ON ingest_pending(workspace_id, api_key_id)
    WHERE workspace_id IS NOT NULL;

CREATE TRIGGER ingest_pending_workspace_required_insert
BEFORE INSERT ON ingest_pending
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest pending workspace_id required');
END;

CREATE TRIGGER ingest_pending_workspace_required_update
BEFORE UPDATE ON ingest_pending
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest pending workspace_id required');
END;

CREATE TRIGGER ingest_pending_workspace_immutable
BEFORE UPDATE OF workspace_id ON ingest_pending
WHEN OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'ingest pending workspace_id immutable');
END;

CREATE TRIGGER ingest_pending_api_key_workspace_insert
BEFORE INSERT ON ingest_pending
WHEN NEW.api_key_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM ingest_api_keys k
   WHERE k.id = NEW.api_key_id
     AND k.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest pending API key workspace mismatch');
END;

CREATE TRIGGER ingest_pending_api_key_workspace_update
BEFORE UPDATE OF workspace_id, api_key_id ON ingest_pending
WHEN NEW.api_key_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM ingest_api_keys k
   WHERE k.id = NEW.api_key_id
     AND k.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest pending API key workspace mismatch');
END;

CREATE TRIGGER ingest_api_keys_pending_workspace_update
BEFORE UPDATE OF workspace_id ON ingest_api_keys
WHEN EXISTS (
  SELECT 1 FROM ingest_pending p
   WHERE p.api_key_id = OLD.id
     AND p.workspace_id != NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest API key pending workspace mismatch');
END;

CREATE TABLE ingest_skipped_v183_new (
    id TEXT PRIMARY KEY,
    workspace_id TEXT,
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

INSERT INTO ingest_skipped_v183_new (
    id, workspace_id, file_path_attempted, project_slug, sha256, reason,
    existing_ingest_id, error_message, created_at, created_by
)
SELECT
    s.id,
    CASE
      -- A declared parent is the authoritative ownership source.  If that
      -- historical reference is dangling or its parent remains unowned, do
      -- not fall back to the project slug and manufacture attribution.
      WHEN s.existing_ingest_id IS NOT NULL THEN (
        SELECT p.workspace_id FROM ingest_pending p
         WHERE p.id = s.existing_ingest_id
      )
      WHEN (
        SELECT COUNT(DISTINCT wp.workspace_id)
          FROM workspace_projects wp
         WHERE wp.project_slug = s.project_slug
           AND length(trim(wp.workspace_id)) > 0
      ) = 1 THEN (
        SELECT MIN(wp.workspace_id)
          FROM workspace_projects wp
         WHERE wp.project_slug = s.project_slug
           AND length(trim(wp.workspace_id)) > 0
      )
      ELSE NULL
    END,
    s.file_path_attempted, s.project_slug, s.sha256, s.reason,
    s.existing_ingest_id, s.error_message, s.created_at, s.created_by
FROM ingest_skipped s;

DROP TABLE ingest_skipped;
ALTER TABLE ingest_skipped_v183_new RENAME TO ingest_skipped;

CREATE INDEX idx_ingest_skipped_workspace_project_created
    ON ingest_skipped(workspace_id, project_slug, created_at DESC)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_skipped_workspace_sha256
    ON ingest_skipped(workspace_id, sha256, project_slug)
    WHERE workspace_id IS NOT NULL;

CREATE TRIGGER ingest_skipped_workspace_required_insert
BEFORE INSERT ON ingest_skipped
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest skipped workspace_id required');
END;

CREATE TRIGGER ingest_skipped_workspace_required_update
BEFORE UPDATE ON ingest_skipped
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest skipped workspace_id required');
END;

CREATE TRIGGER ingest_skipped_workspace_immutable
BEFORE UPDATE OF workspace_id ON ingest_skipped
WHEN OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'ingest skipped workspace_id immutable');
END;

CREATE TRIGGER ingest_skipped_parent_workspace_insert
BEFORE INSERT ON ingest_skipped
WHEN NEW.existing_ingest_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM ingest_pending p
   WHERE p.id = NEW.existing_ingest_id
     AND p.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest skipped parent workspace mismatch');
END;

CREATE TRIGGER ingest_skipped_parent_workspace_update
BEFORE UPDATE OF workspace_id, existing_ingest_id ON ingest_skipped
WHEN NEW.existing_ingest_id IS NOT NULL AND NOT EXISTS (
  SELECT 1 FROM ingest_pending p
   WHERE p.id = NEW.existing_ingest_id
     AND p.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest skipped parent workspace mismatch');
END;

CREATE TABLE ingest_change_history_v183_new (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT,
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

INSERT INTO ingest_change_history_v183_new (
    id, workspace_id, ingest_pending_id, field_name, old_value, new_value,
    changed_by, changed_at, source_ip, user_agent
)
SELECT
    h.id,
    (SELECT p.workspace_id FROM ingest_pending p WHERE p.id = h.ingest_pending_id),
    h.ingest_pending_id, h.field_name, h.old_value, h.new_value,
    h.changed_by, h.changed_at, h.source_ip, h.user_agent
FROM ingest_change_history h;

DROP TABLE ingest_change_history;
ALTER TABLE ingest_change_history_v183_new RENAME TO ingest_change_history;

CREATE INDEX idx_change_hist_workspace_ingest_id
    ON ingest_change_history(workspace_id, ingest_pending_id, changed_at DESC)
    WHERE workspace_id IS NOT NULL;

CREATE TRIGGER ingest_change_history_workspace_required_insert
BEFORE INSERT ON ingest_change_history
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest change history workspace_id required');
END;

CREATE TRIGGER ingest_change_history_workspace_required_update
BEFORE UPDATE ON ingest_change_history
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest change history workspace_id required');
END;

CREATE TRIGGER ingest_change_history_workspace_immutable
BEFORE UPDATE OF workspace_id ON ingest_change_history
WHEN OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'ingest change history workspace_id immutable');
END;

CREATE TRIGGER ingest_change_history_parent_workspace_insert
BEFORE INSERT ON ingest_change_history
WHEN NOT EXISTS (
  SELECT 1 FROM ingest_pending p
   WHERE p.id = NEW.ingest_pending_id
     AND p.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest change history parent workspace mismatch');
END;

CREATE TRIGGER ingest_change_history_parent_workspace_update
BEFORE UPDATE OF workspace_id, ingest_pending_id ON ingest_change_history
WHEN NOT EXISTS (
  SELECT 1 FROM ingest_pending p
   WHERE p.id = NEW.ingest_pending_id
     AND p.workspace_id = NEW.workspace_id
)
BEGIN
  SELECT RAISE(ABORT, 'ingest change history parent workspace mismatch');
END;

CREATE TABLE ingest_webhook_nonces_v183_new (
    workspace_id TEXT,
    source TEXT NOT NULL,
    nonce TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT INTO ingest_webhook_nonces_v183_new (
    workspace_id, source, nonce, request_sha256, received_at
)
SELECT NULL, source, nonce, request_sha256, received_at
FROM ingest_webhook_nonces;

DROP TABLE ingest_webhook_nonces;
ALTER TABLE ingest_webhook_nonces_v183_new RENAME TO ingest_webhook_nonces;

CREATE UNIQUE INDEX idx_ingest_webhook_nonces_workspace_source_nonce
    ON ingest_webhook_nonces(workspace_id, source, nonce)
    WHERE workspace_id IS NOT NULL;
CREATE INDEX idx_ingest_webhook_nonces_workspace_received
    ON ingest_webhook_nonces(workspace_id, received_at DESC)
    WHERE workspace_id IS NOT NULL;

CREATE TRIGGER ingest_webhook_nonces_workspace_required_insert
BEFORE INSERT ON ingest_webhook_nonces
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest webhook nonce workspace_id required');
END;

CREATE TRIGGER ingest_webhook_nonces_workspace_required_update
BEFORE UPDATE ON ingest_webhook_nonces
WHEN NEW.workspace_id IS NULL OR length(trim(NEW.workspace_id)) = 0
BEGIN
  SELECT RAISE(ABORT, 'ingest webhook nonce workspace_id required');
END;

CREATE TRIGGER ingest_webhook_nonces_workspace_immutable
BEFORE UPDATE OF workspace_id ON ingest_webhook_nonces
WHEN OLD.workspace_id IS NOT NULL AND OLD.workspace_id != NEW.workspace_id
BEGIN
  SELECT RAISE(ABORT, 'ingest webhook nonce workspace_id immutable');
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (183);

COMMIT;
PRAGMA foreign_keys=ON;
