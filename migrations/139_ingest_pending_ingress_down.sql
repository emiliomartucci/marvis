-- Rollback migration 139: drop ingress columns + 'api_ingress' source kind.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U2
--
-- SQLite cannot drop a CHECK value in place, so this is a reverse full rebuild
-- back to the pre-139 (mig 126) DDL. Any 'api_ingress' rows are first demoted to
-- 'file_drop' so they survive the restored CHECK. Same foreign_keys=OFF envelope
-- as the up migration (protects ingest_change_history / ingest_skipped FKs).

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

UPDATE ingest_pending SET source_kind = 'file_drop' WHERE source_kind = 'api_ingress';

CREATE TABLE ingest_pending_old_139 (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    project_slug TEXT NOT NULL DEFAULT 'unclassified',
    source_kind TEXT NOT NULL DEFAULT 'file_drop' CHECK(source_kind IN (
        'file_drop', 'manual_upload', 'api_upload', 'terminal_upload'
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
    UNIQUE(sha256, project_slug)
);

INSERT INTO ingest_pending_old_139 (
    id, file_path, sha256, project_slug, source_kind, mime_type,
    file_size_bytes, parser_used, extracted_text, structure_json,
    classification_json, status, error_message, triage_decision_id,
    target_folder, target_filename, created_at, updated_at,
    recovery_attempts, locked_by, locked_at
)
SELECT
    id, file_path, sha256, project_slug, source_kind, mime_type,
    file_size_bytes, parser_used, extracted_text, structure_json,
    classification_json, status, error_message, triage_decision_id,
    target_folder, target_filename, created_at, updated_at,
    recovery_attempts, locked_by, locked_at
FROM ingest_pending;

DROP TABLE ingest_pending;
ALTER TABLE ingest_pending_old_139 RENAME TO ingest_pending;

CREATE INDEX idx_ingest_pending_status ON ingest_pending(status);
CREATE INDEX idx_ingest_pending_project ON ingest_pending(project_slug);
CREATE INDEX idx_ingest_pending_created ON ingest_pending(created_at DESC);
CREATE INDEX idx_ingest_pending_lock ON ingest_pending(locked_at)
    WHERE locked_at IS NOT NULL;

DELETE FROM schema_versions WHERE version = 139;

COMMIT;

PRAGMA foreign_keys=ON;
