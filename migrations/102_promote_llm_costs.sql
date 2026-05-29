-- v1.1.0 - 2026-05-03 - Renumbered 100 -> 102 (collision with 100_kg_enriched_at).
-- v1.0.0 - 2026-04-30 - Promote llm_costs from lazy-create to formal migration
--
-- 2026-05-03 history: file was originally 100_promote_llm_costs.sql but the
-- alphabetic-sort migration runner applied 100_kg_enriched_at.sql first,
-- stamped schema_versions=100, then skipped 100_promote_llm_costs (since
-- version > current_version was now false). Result: console.db.llm_costs
-- never gained tier_logical/fallback_used/litellm_request_id, breaking the
-- inbox_llm_classifier insert path for ~24h. Renumbered to 102 so the
-- sorter applies it on next boot. Hook trigger in api/db.py moved to
-- version==102. Learning ref: 09dc6b55.
--
-- Until now `llm_costs` was lazy-created by inbox_llm_classifier._ensure_llm_costs_table().
-- That helper shipped the table only after the classifier path had been exercised at
-- least once, leaving fresh DB bootstrap + integration tests + Triage onboarding
-- without the table. Promote to a formal migration so every environment has the
-- schema from boot.
--
-- The schema preserves the existing column types already in production rows
-- (id TEXT, created_at TEXT ISO8601). Three new nullable columns are added for
-- MAC-Phase 0.5 LiteLLM gateway:
--
--   - tier_logical TEXT          : logical model name (tier-think|tier-fast|...) or NULL for legacy.
--   - fallback_used INTEGER      : 0/1 flag, set when LiteLLM falls back from local Mac to cloud.
--   - litellm_request_id TEXT    : LiteLLM correlation id, for cross-DB debug
--                                  (Hetzner llm_costs ↔ Mac LiteLLM_SpendLogs).
--
-- WARNING: NEVER add prompt or response columns to llm_costs. PII / GDPR Art. 32
-- violation. Plain-text prompts/responses must live only in rotated file logs
-- with pii_redactor applied (retention <= 7 days, chmod 600).
--
-- Idempotency strategy:
--   - This SQL file does the safe-on-rerun parts (CREATE TABLE IF NOT EXISTS,
--     CREATE INDEX IF NOT EXISTS, schema_versions stamp).
--   - The post-migration Python hook `_promote_llm_costs_columns()` in api/db.py
--     handles ALTER TABLE ADD COLUMN for the 3 new columns, guarded by
--     `_column_exists()` so it is safe on both fresh DBs (where CREATE already
--     installed them) and prod DBs (where the lazy-created old schema lacked them).
--
-- DEPLOY PROCEDURE (auto via run_migrations at startup):
--   1. Deploy api → /data/pir/migrations/100_promote_llm_costs.sql in place.
--   2. systemctl --user restart pir-api.service.
--   3. run_migrations() applies version 100, then runs the post-hook.
--
-- Reversible: vedi migrations/100_promote_llm_costs_down.sql (no-op, SQLite
-- pre-3.35 cannot DROP COLUMN; documented).

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS llm_costs (
    id TEXT PRIMARY KEY,
    feature TEXT NOT NULL,
    model TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd REAL NOT NULL,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at TEXT DEFAULT (datetime('now','utc')),
    tier_logical TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    litellm_request_id TEXT
);

CREATE INDEX IF NOT EXISTS idx_llm_costs_feature_created
    ON llm_costs(feature, created_at, workspace_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (102);

COMMIT;
