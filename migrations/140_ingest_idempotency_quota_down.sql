-- Rollback migration 140: drop idempotency + rate/quota counters.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U2

DROP INDEX IF EXISTS idx_ingest_idempotency_created;
DROP TABLE IF EXISTS ingest_rate_usage;
DROP TABLE IF EXISTS ingest_quota_usage;
DROP TABLE IF EXISTS ingest_idempotency;

DELETE FROM schema_versions WHERE version = 140;
