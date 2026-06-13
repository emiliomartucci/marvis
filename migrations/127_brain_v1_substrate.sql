-- Migration 127: Brain v1 substrate (sub-01 Digest + Journal)
-- Date: 2026-05-16
-- Plan: docs/plans/sub/2026-05-15-brain-v1-01-digest-journal.md
-- Parent: docs/plans/2026-05-15-feat-brain-v1-mielinizzazione-plan.md
-- Author: brain-v1
--
-- Tables:
--   brain_runs              -- cycle envelope (status machine)
--   brain_source_watermarks -- incremental scan cursors per source
--   brain_digest_events     -- append-only event observations (BLAKE2b stable ids)
--   brain_journal_entries   -- materialized aggregations per scope
--
-- Invariants:
--   * event_id is a BLAKE2b stable hash (NOT UUID); composite UK guards idempotency.
--   * No FK into mutable substrate (tasks/pull_requests/handoffs/etc).
--   * brain_digest_events.run_id FK CASCADE; brain_journal_entries.run_id FK CASCADE.
--   * brain_digest_events is write-once (no updated_at trigger).
--   * Partial unique index enforces at most one active run per (cycle_key).
--   * ISO-8601 with milliseconds timestamps (mig 097 convention).
--
-- Rollback: migrations/127_brain_v1_substrate_down.sql
-- Apply:    sqlite3 /data/pir/console.db < migrations/127_brain_v1_substrate.sql

BEGIN IMMEDIATE;

-- ------------------------------------------------------------------
-- brain_runs : cycle envelope
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_runs (
    run_id                  TEXT PRIMARY KEY,
    workspace_id            TEXT NOT NULL DEFAULT 'ws_default',
    cycle_key               TEXT NOT NULL,
    cycle_window_start_utc  TEXT NOT NULL,
    cycle_window_end_utc    TEXT NOT NULL,
    cutoff_hour_utc_at_run  INTEGER NOT NULL,
    scope_type              TEXT NOT NULL DEFAULT 'company'
                            CHECK(scope_type IN ('company')),
    scope_key               TEXT NOT NULL DEFAULT '__company__',
    trigger                 TEXT NOT NULL
                            CHECK(trigger IN ('batch', 'manual', 'backfill')),
    triggered_by            TEXT,
    started_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    finished_at             TEXT,
    status                  TEXT NOT NULL DEFAULT 'running'
                            CHECK(status IN ('running', 'succeeded', 'partial', 'failed', 'superseded')),
    -- superseded_by_run_id is a self-pointer for audit trail. We deliberately
    -- omit the self-referencing FK: it forces a 3-phase insert (mark old →
    -- insert new → update pointer) which is brittle, and the partial unique
    -- index plus the status enum already protect the invariant.
    superseded_by_run_id    TEXT,
    event_count             INTEGER NOT NULL DEFAULT 0,
    partial_failures_json   TEXT NOT NULL DEFAULT '[]'
                            CHECK(json_valid(partial_failures_json)),
    duration_ms             INTEGER,
    error_summary           TEXT,
    updated_at              TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_brain_runs_cycle_status
    ON brain_runs(cycle_key, status);

CREATE INDEX IF NOT EXISTS idx_brain_runs_running
    ON brain_runs(status, started_at)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_brain_runs_triggered
    ON brain_runs(triggered_by, started_at DESC);

-- One active run per cycle (recompute marks old run superseded first).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_brain_runs_active_cycle
    ON brain_runs(workspace_id, cycle_key)
    WHERE status IN ('running', 'succeeded') AND superseded_by_run_id IS NULL;

CREATE TRIGGER IF NOT EXISTS trg_brain_runs_updated_at
    AFTER UPDATE ON brain_runs
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_runs
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE run_id = NEW.run_id;
END;

-- ------------------------------------------------------------------
-- brain_source_watermarks : incremental scan cursors
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_source_watermarks (
    source_system     TEXT NOT NULL
                      CHECK(source_system IN (
                          'ingest', 'git', 'kg', 'pir',
                          'handoff', 'learning', 'ci', 'docs_governance'
                      )),
    workspace_id      TEXT NOT NULL DEFAULT 'ws_default',
    last_observed_at  TEXT NOT NULL,
    last_event_id     TEXT,
    last_cycle_key    TEXT,
    updated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (source_system, workspace_id)
);

-- ------------------------------------------------------------------
-- brain_digest_events : append-only observation log
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_digest_events (
    event_id               TEXT PRIMARY KEY,
    run_id                 TEXT NOT NULL,
    cycle_key              TEXT NOT NULL,
    observed_at            TEXT NOT NULL
                           CHECK(observed_at LIKE '%Z' OR observed_at LIKE '%+00:00'),
    derived_from_state_at  TEXT NOT NULL,
    event_type             TEXT NOT NULL
                           CHECK(event_type IN (
                               'file_changed', 'commit_changed', 'task_changed', 'pr_changed',
                               'handoff_changed', 'learning_changed', 'doc_changed',
                               'ingest_changed', 'kg_changed', 'regression_signal',
                               'external_update_seen'
                           )),
    schema_version         INTEGER NOT NULL DEFAULT 1,
    source_system          TEXT NOT NULL
                           CHECK(source_system IN (
                               'ingest', 'git', 'kg', 'pir',
                               'handoff', 'learning', 'ci', 'docs_governance'
                           )),
    source_project         TEXT,
    target_project         TEXT,
    program_key            TEXT,
    source_ref             TEXT NOT NULL,
    title                  TEXT NOT NULL,
    summary                TEXT NOT NULL DEFAULT '',
    evidence_json          TEXT NOT NULL DEFAULT '{}'
                           CHECK(json_valid(evidence_json)),
    evidence_hash          TEXT NOT NULL
                           CHECK(length(evidence_hash) = 64),
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE CASCADE
);

-- Composite natural key for idempotency. Drives INSERT OR IGNORE on recompute.
CREATE UNIQUE INDEX IF NOT EXISTS uniq_brain_digest_events_nat
    ON brain_digest_events(cycle_key, event_type, source_ref, evidence_hash);

CREATE INDEX IF NOT EXISTS idx_brain_digest_events_cycle_source_project
    ON brain_digest_events(cycle_key, source_project);

CREATE INDEX IF NOT EXISTS idx_brain_digest_events_source_ref
    ON brain_digest_events(source_ref);

CREATE INDEX IF NOT EXISTS idx_brain_digest_events_run
    ON brain_digest_events(run_id);

CREATE INDEX IF NOT EXISTS idx_brain_digest_events_type_cycle
    ON brain_digest_events(event_type, cycle_key);

-- Cursor index for D6 events read API (observed_at DESC, event_id).
CREATE INDEX IF NOT EXISTS idx_brain_digest_events_cursor
    ON brain_digest_events(observed_at DESC, event_id);

-- ------------------------------------------------------------------
-- brain_journal_entries : materialized aggregation per scope
-- ------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS brain_journal_entries (
    entry_id       TEXT PRIMARY KEY,
    run_id         TEXT NOT NULL,
    workspace_id   TEXT NOT NULL DEFAULT 'ws_default',
    cycle_key      TEXT NOT NULL,
    scope_type     TEXT NOT NULL
                   CHECK(scope_type IN ('company', 'program', 'project')),
    scope_key      TEXT NOT NULL,
    program_key    TEXT,
    body_json      TEXT NOT NULL
                   CHECK(json_valid(body_json)),
    is_empty       INTEGER NOT NULL DEFAULT 0
                   CHECK(is_empty IN (0, 1)),
    published_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (run_id) REFERENCES brain_runs(run_id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS uniq_brain_journal_entries_run_scope
    ON brain_journal_entries(run_id, scope_type, scope_key);

CREATE INDEX IF NOT EXISTS idx_brain_journal_entries_timeline
    ON brain_journal_entries(workspace_id, scope_type, scope_key, cycle_key DESC);

CREATE INDEX IF NOT EXISTS idx_brain_journal_entries_cycle_scope
    ON brain_journal_entries(cycle_key, scope_type, scope_key);

CREATE TRIGGER IF NOT EXISTS trg_brain_journal_entries_updated_at
    AFTER UPDATE ON brain_journal_entries
    FOR EACH ROW
    WHEN NEW.updated_at = OLD.updated_at
BEGIN
    UPDATE brain_journal_entries
       SET updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')
     WHERE entry_id = NEW.entry_id;
END;

INSERT OR IGNORE INTO schema_versions(version) VALUES (127);

COMMIT;
