-- Migration 140: request idempotency + per-key rate/quota counters.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U2
--
-- All three tables are written exclusively through the single SQLite writer
-- (acquire_write_db / write_db, learning 6130bc49). The counters are claimed as
-- atomic check-and-increment statements (UPDATE ... WHERE count < limit) so two
-- concurrent requests cannot both pass the gate. Stale rows are pruned by the
-- hourly _periodic_cleanup sweep (sleep-before-lock, learning 4d4278e4).
--
-- Rollback: migrations/140_ingest_idempotency_quota_down.sql

-- Request-level dedup: an Idempotency-Key is claimed (status='pending') as the
-- FIRST atomic write; the winner finalizes (status='done' + response snapshot),
-- replays return the snapshot. request_sha256 binds the key to its payload so a
-- reused key with a different body is a 422, not a stale replay.
CREATE TABLE IF NOT EXISTS ingest_idempotency (
    api_key_id     TEXT NOT NULL,
    idem_key       TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'pending'
                     CHECK(status IN ('pending', 'done')),
    response_json  TEXT,                              -- snapshot returned on replay (NULL while pending)
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (api_key_id, idem_key)
);
CREATE INDEX IF NOT EXISTS idx_ingest_idempotency_created ON ingest_idempotency(created_at);

-- Per-key daily quota counter (durable abuse backstop).
CREATE TABLE IF NOT EXISTS ingest_quota_usage (
    api_key_id  TEXT NOT NULL,
    usage_date  TEXT NOT NULL,                        -- YYYY-MM-DD UTC
    count       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, usage_date)
);

-- Per-key per-minute rate counter (durable, multi-worker-safe — slowapi's
-- in-memory limiter is best-effort and does not survive restart or span workers).
CREATE TABLE IF NOT EXISTS ingest_rate_usage (
    api_key_id   TEXT NOT NULL,
    minute_bucket TEXT NOT NULL,                      -- YYYY-MM-DDTHH:MM UTC
    count        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (api_key_id, minute_bucket)
);

INSERT OR IGNORE INTO schema_versions(version) VALUES (140);
