-- Migration 100 — Phase 1.5 E6: kg_enriched_at column for background enrichment
--
-- Adds tracking column on graph_nodes per saber se LLM #2 (kg_enricher) e' gia'
-- stato eseguito. NULL = pending, datetime = enrichment completed.
--
-- Cron Phase 2 future puo' filtrare WHERE kg_enriched_at IS NULL per re-trigger.
-- Partial index su NULL ottimizza lo sweep cron.
--
-- DEPLOY PROCEDURE (auto via run_migrations al startup):
--   1. Deploy api → /data/pir/migrations/100*.sql sincronizzata
--   2. systemctl restart pir-api → run_migrations applica
--
-- Reversibile: vedi 100_kg_enriched_at_down.sql.

BEGIN IMMEDIATE;

ALTER TABLE graph_nodes ADD COLUMN kg_enriched_at TEXT;

CREATE INDEX IF NOT EXISTS idx_graph_nodes_enriched_pending
    ON graph_nodes(created_at)
    WHERE kg_enriched_at IS NULL AND deprecated_at IS NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (100);

COMMIT;
