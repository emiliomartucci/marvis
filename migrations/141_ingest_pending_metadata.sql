-- Migration 141: persist ingress payload metadata on ingest_pending.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U3
--
-- The JSON ingress payload may carry free-form `metadata`. Fase 1 (U1/U2)
-- accepted it but did not persist it (the parser owns structure_json). U3 stores
-- it on the row at enqueue time so parse_pending can reconcile it into the final
-- structure_json (one canonical place for downstream KG / Console consumers).
--
-- Plain ADD COLUMN (nullable, no CHECK change) → no table rebuild needed.
-- Owner-surface rows leave it NULL.
--
-- Rollback: migrations/141_ingest_pending_metadata_down.sql

ALTER TABLE ingest_pending ADD COLUMN ingress_metadata TEXT;

INSERT OR IGNORE INTO schema_versions(version) VALUES (141);
