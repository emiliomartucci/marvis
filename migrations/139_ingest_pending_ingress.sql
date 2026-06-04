-- Migration 139: ingress columns + 'api_ingress' source kind on ingest_pending.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U2
--
-- SQLite cannot alter a CHECK constraint in place, so ingest_pending is rebuilt
-- (same pattern as mig 126). Three governance columns are added in the same
-- rebuild (api_key_id, source, ingest_policy) — declared in the new-table DDL,
-- NOT via separate ALTER ADD COLUMN (a CHECK change forces a full rebuild, so a
-- standalone ALTER would be dead/conflicting). The ~324 historical rows keep
-- these columns NULL (owner drops, no API key) — correct, no backfill needed.
--
-- api_key_id is bare TEXT by design (no FK): key revocation is a soft-delete
-- (ingest_api_keys.revoked_at, never a row DELETE), so no dangling reference can
-- ever occur and no ON DELETE rule is required.
--
-- The PRAGMA foreign_keys=OFF envelope (mirrors mig 126) is mandatory: without
-- it, DROP TABLE ingest_pending cascade-deletes ingest_change_history (FK
-- ON DELETE CASCADE, mig 097 — the triage audit trail) and NULLs
-- ingest_skipped.existing_ingest_id (FK, mig 103). Invariant: row counts of
-- ingest_change_history / ingest_skipped are unchanged across this migration.
--
-- Rollback: migrations/139_ingest_pending_ingress_down.sql

PRAGMA foreign_keys=OFF;

BEGIN IMMEDIATE;

CREATE TABLE ingest_pending_new_139 (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    project_slug TEXT NOT NULL DEFAULT 'unclassified',
    source_kind TEXT NOT NULL DEFAULT 'file_drop' CHECK(source_kind IN (
        'file_drop', 'manual_upload', 'api_upload', 'terminal_upload', 'api_ingress'
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
    -- M1 CAPTURE ingress columns (NULL for owner drops):
    api_key_id TEXT,            -- ingest_api_keys.id (bare TEXT, soft-delete keys → no dangling ref)
    source TEXT,                -- payload source label (provenance / filtering)
    ingest_policy TEXT,         -- snapshot of key policy at intake ('open' | 'trusted')
    UNIQUE(sha256, project_slug)
);

INSERT INTO ingest_pending_new_139 (
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
ALTER TABLE ingest_pending_new_139 RENAME TO ingest_pending;

CREATE INDEX idx_ingest_pending_status ON ingest_pending(status);
CREATE INDEX idx_ingest_pending_project ON ingest_pending(project_slug);
CREATE INDEX idx_ingest_pending_created ON ingest_pending(created_at DESC);
CREATE INDEX idx_ingest_pending_lock ON ingest_pending(locked_at)
    WHERE locked_at IS NOT NULL;
CREATE INDEX idx_ingest_pending_source ON ingest_pending(source);
CREATE INDEX idx_ingest_pending_api_key ON ingest_pending(api_key_id);

INSERT OR IGNORE INTO schema_versions(version) VALUES (139);

COMMIT;

PRAGMA foreign_keys=ON;
