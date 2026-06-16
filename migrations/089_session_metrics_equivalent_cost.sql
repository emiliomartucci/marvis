-- Migration 089 — Shadow "cost_equivalent" columns
--
-- Piano di riferimento: docs/plans/2026-04-22-feat-metrics-provider-consistency-plan.md (PR4).
--
-- Goal: aggiungere a `sessions_meta` tre colonne per la "shadow cost":
-- quanto la sessione SAREBBE costata a pay-per-token API anche quando il
-- cost reale e' 0 (OAuth / free tier, es. openai/gpt-5.4 via ChatGPT Plus).
--
-- Rationale: molti modelli OpenCode (OpenAI OAuth, Groq free tier) riportano
-- `message.data.cost = 0` → il conteggio dei token e' comunque utile per
-- capire volume reale. Rimaniamo backward-compatible: `last_cost_*` resta
-- il costo REALE, `last_cost_*_equivalent_usd` e' il costo shadow.
--
-- `last_cost_equivalent_pricing_version` traccia quale versione di
-- `kb/opencode-pricing-*.json` ha generato il valore (audit).
--
-- SQLite 3.35+ supporta `ALTER TABLE ... DROP COLUMN` nativo.

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta ADD COLUMN last_cost_conversation_equivalent_usd REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_session_equivalent_usd REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_equivalent_pricing_version TEXT;

INSERT OR IGNORE INTO schema_versions (version) VALUES (89);
COMMIT;
