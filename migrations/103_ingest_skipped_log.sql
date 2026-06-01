-- v1.0.0 - 2026-05-03 - UX-6 ingest skipped tracking + categoria Ignorati
--
-- Tabella di audit per file silenziosamente skippati durante upload:
--   - dedup_sha256: file gia' presente in ingest_pending con stesso (sha256, project)
--   - invalid_path: filename con caratteri rifiutati o path traversal
--   - mime_not_allowed: estensione/MIME fuori whitelist (es .exe, .mp3 pre-Phase 2.5)
--   - parse_error_pre_dispatch: parser_router decide non parsare prima di entrare saga
--
-- Source of truth per la sidebar "Ignorati" e per popolare dedup_files[] nel
-- response /upload-folder. Cosi' il frontend distingue:
--   - 200 + queued > 0 → "done in pipeline"
--   - 200 + queued = 0 + dedup_files non vuoto → "dedup silenzioso"
--   - 4xx + skipped_files con reason → "rifiutato"
--
-- Idempotency: pure CREATE TABLE IF NOT EXISTS + indici.
-- NO data migration (tabella nuova, vuota al boot).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS ingest_skipped (
    id TEXT PRIMARY KEY,
    file_path_attempted TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    sha256 TEXT,
    reason TEXT NOT NULL CHECK (reason IN (
        'dedup_sha256',
        'invalid_path',
        'mime_not_allowed',
        'parse_error_pre_dispatch'
    )),
    existing_ingest_id TEXT,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    created_by TEXT,
    FOREIGN KEY (existing_ingest_id) REFERENCES ingest_pending(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_ingest_skipped_project_created
    ON ingest_skipped(project_slug, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_ingest_skipped_sha256
    ON ingest_skipped(sha256, project_slug);

INSERT OR IGNORE INTO schema_versions (version) VALUES (103);

COMMIT;
