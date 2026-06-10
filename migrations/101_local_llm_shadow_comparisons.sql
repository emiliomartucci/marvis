-- v1.0.0 - 2026-04-30 - Shadow-mode comparisons table for cloud vs local LLM
--
-- MAC-Phase 0.5 introduce un gateway LiteLLM sul Mac Studio che espone i modelli
-- locali (tier-think Qwen 35B distill, tier-fast Qwen 3B) via OpenAI-compatible
-- endpoint. Prima di routare il traffico user-facing al locale, eseguiamo per
-- 1 settimana shadow mode: la response cloud (Sonnet) viene applicata all'utente,
-- in parallelo si chiama Mac e si logga la comparison. Eyeball review +
-- judge LLM A/B decide il go/no-go.
--
-- WARNING: NEVER persist plain-text prompt or response in this table. PII / GDPR
-- Art. 32. Use sha256 hash + rotated file logs (chmod 600, retention <= 7gg)
-- for any judge sample manuale. Hash garantisce comparability senza ritenere
-- il contenuto.
--
-- Schema:
--   id                 : uuid stringa, primary key
--   item_id            : foreign-key-soft a inbox_items.id (no FK constraint per
--                        permettere comparisons orfane se l'item viene poi cancellato).
--   feature            : 'inbox_tldr' | 'inbox_deep_research' | future workflow names.
--   model_cloud        : nome modello cloud (es 'claude-sonnet-4-20250514').
--   model_local        : tier logico (es 'tier-think').
--   cloud_text_hash    : sha256 hex della response cloud (NON plain text).
--   local_text_hash    : sha256 hex della response locale.
--   *_tokens_in/out    : token counts from provider response.usage.
--   *_latency_ms       : end-to-end latency including network.
--   *_cost_usd         : cost stimato.
--   judge_score        : 0-1, populated da judge LLM A/B (Phase 1.0+, NULL inizialmente).
--   judge_verdict      : free-text reasoning judge, NULL inizialmente.
--   workspace_id       : multi-tenant scoping.
--   created_at         : unix epoch (INTEGER) per lookup time-window efficient.
--
-- DEPLOY PROCEDURE: idem migration 100. Auto via run_migrations.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS local_llm_shadow_comparisons (
    id TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    feature TEXT NOT NULL,
    model_cloud TEXT NOT NULL,
    model_local TEXT NOT NULL,
    cloud_text_hash TEXT NOT NULL,
    local_text_hash TEXT NOT NULL,
    cloud_tokens_in INTEGER,
    cloud_tokens_out INTEGER,
    cloud_latency_ms INTEGER,
    local_tokens_in INTEGER,
    local_tokens_out INTEGER,
    local_latency_ms INTEGER,
    cloud_cost_usd REAL,
    local_cost_usd REAL,
    judge_score REAL,
    judge_verdict TEXT,
    workspace_id TEXT NOT NULL DEFAULT 'ws_default',
    created_at INTEGER NOT NULL DEFAULT (strftime('%s','now'))
);

CREATE INDEX IF NOT EXISTS idx_shadow_feature_created
    ON local_llm_shadow_comparisons(feature, created_at);

CREATE INDEX IF NOT EXISTS idx_shadow_item
    ON local_llm_shadow_comparisons(item_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (101);

COMMIT;
