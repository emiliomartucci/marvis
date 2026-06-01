-- Migration 124: heypocket_recordings state machine
-- Date: 2026-05-12
-- Plan: docs/plans/2026-05-12-feat-heypocket-recordings-poller-plan.md
-- Author: codex

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS heypocket_recordings (
    recording_id       TEXT PRIMARY KEY,
    title              TEXT,
    duration_seconds   INTEGER,
    recording_at       TEXT,
    created_at_pocket  TEXT NOT NULL,
    state              TEXT NOT NULL DEFAULT 'fetched'
                       CHECK(state IN (
                           'fetched',
                           'metadata_written',
                           'downloading',
                           'download_failed',
                           'dropped',
                           'queued',
                           'ingested',
                           'parse_error',
                           'unreachable',
                           'skipped'
                       )),
    audio_sha256       TEXT,
    audio_path         TEXT,
    metadata_path      TEXT,
    audio_extension    TEXT,
    ingest_pending_id  TEXT,
    last_error         TEXT,
    retry_count        INTEGER NOT NULL DEFAULT 0,
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now')),
    ingested_at        TEXT,
    cleaned_at         TEXT,
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at         TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_heypocket_recordings_state
    ON heypocket_recordings(state);

CREATE INDEX IF NOT EXISTS idx_heypocket_recordings_cursor
    ON heypocket_recordings(created_at_pocket DESC);

CREATE INDEX IF NOT EXISTS idx_heypocket_recordings_sha256
    ON heypocket_recordings(audio_sha256)
    WHERE audio_sha256 IS NOT NULL;

CREATE TRIGGER IF NOT EXISTS trg_heypocket_recordings_updated_at
    AFTER UPDATE ON heypocket_recordings
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE heypocket_recordings
       SET updated_at = datetime('now')
     WHERE recording_id = NEW.recording_id;
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (124);

COMMIT;
