-- Migration 122: docs_drift_history table for Plan 7 Gap B V1 dedup
-- Date: 2026-05-11
-- Plan: P7 — Drift Cron Post-Commit (V1)
-- Author: marvisx

BEGIN IMMEDIATE;

CREATE TABLE docs_drift_history (
    id INTEGER PRIMARY KEY,
    check_name TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK(length(fingerprint) = 64),
    drift_detail TEXT NOT NULL CHECK(json_valid(drift_detail)),
    opened_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    dedup_expires_at TEXT DEFAULT (datetime('now', '+7 days')),
    CHECK(dedup_expires_at IS NULL OR datetime(dedup_expires_at) IS NOT NULL)
);

-- Atomic dedup via UNIQUE partial index.
-- SQLite rejects non-deterministic predicates like datetime('now', '-7 days')
-- during INSERT, so Python expires old rows by setting dedup_expires_at=NULL
-- before INSERT OR IGNORE.
CREATE UNIQUE INDEX idx_drift_fingerprint_open
    ON docs_drift_history(fingerprint)
    WHERE dedup_expires_at IS NOT NULL;

CREATE INDEX idx_drift_opened_task
    ON docs_drift_history(opened_task_id)
    WHERE opened_task_id IS NOT NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (122);

COMMIT;
