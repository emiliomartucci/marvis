-- Rollback migration 141: drop ingest_pending.ingress_metadata.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U3
-- SQLite >= 3.35 supports DROP COLUMN (live is 3.45.1).

ALTER TABLE ingest_pending DROP COLUMN ingress_metadata;

DELETE FROM schema_versions WHERE version = 141;
