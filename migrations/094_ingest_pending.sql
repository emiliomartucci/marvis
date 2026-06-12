-- Migration 094: ingest_pending table for Universal Ingestion Manager phase 1
-- v1.0.0 - 2026-04-27
--
-- Prerequisite: none. This is an additive table and does not lock existing
-- high-churn tables.
-- Rollback: migrations/094_ingest_pending_down.sql
-- Apply: sqlite3 /data/pir/console.db < migrations/094_ingest_pending.sql
--
-- Notes:
--   - UNIQUE(sha256, project_slug) makes watcher retries idempotent while still
--     allowing the same file content to be ingested in different projects.
--   - The recovery/lock columns are intentionally present before E5.1 so later
--     recovery jobs do not need a retrofit migration.

BEGIN IMMEDIATE;

CREATE TABLE ingest_pending (
    id TEXT PRIMARY KEY,
    file_path TEXT NOT NULL,
    sha256 TEXT NOT NULL,
    project_slug TEXT NOT NULL DEFAULT 'unclassified',
    source_kind TEXT NOT NULL DEFAULT 'file_drop' CHECK(source_kind IN (
        'file_drop', 'manual_upload', 'api_upload'
    )),
    mime_type TEXT,
    file_size_bytes INTEGER,
    parser_used TEXT,
    extracted_text TEXT,
    structure_json TEXT,
    classification_json TEXT,
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
        'queued', 'parsing', 'classified', 'awaiting_triage',
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

CREATE INDEX idx_ingest_pending_status ON ingest_pending(status);
CREATE INDEX idx_ingest_pending_project ON ingest_pending(project_slug);
CREATE INDEX idx_ingest_pending_created ON ingest_pending(created_at DESC);
CREATE INDEX idx_ingest_pending_lock ON ingest_pending(locked_at)
    WHERE locked_at IS NOT NULL;

INSERT OR IGNORE INTO schema_versions(version) VALUES (94);

COMMIT;
