-- Migration 087 — Dual metrics columns + session_conversations normalized table
--
-- Piano di riferimento: docs/plans/2026-04-22-feat-metrics-provider-consistency-plan.md (PR2).
--
-- Goal: estendere `sessions_meta` con metriche dual-scope (conversation vs
-- session cumulativo su chain di resume) + tabella normalizzata
-- `session_conversations` per tracciare la chain di resume (append-only).
--
-- Rationale (vedi Enhancement Summary §4): denormalizzare `conversation_ids`
-- come JSON array introduce race condition su read-modify-write al resume.
-- Una tabella dedicata con PK composta rende l'append idempotente
-- (INSERT OR IGNORE) senza JSON scan.
--
-- Backward-compat: le colonne legacy (`last_cost_usd`, `last_context_pct`)
-- restano popolate dal loop per continuita' del contratto API durante rollout.
-- Computed fields nei Pydantic model fanno l'aliasing fino a PR3.
--
-- SQLite 3.35+ supporta `ALTER TABLE ... DROP COLUMN` nativo; il down-migration
-- puo' fare drop diretti colonna-per-colonna (server ha 3.45+).

BEGIN IMMEDIATE;

-- Cached metrics on sessions_meta (1:1 con session, rinfrescati dal loop ogni 2m)
ALTER TABLE sessions_meta ADD COLUMN last_context_pct_real REAL;
ALTER TABLE sessions_meta ADD COLUMN last_context_pct_scaled REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_conversation_usd REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_session_usd REAL;
ALTER TABLE sessions_meta ADD COLUMN last_cost_session_incomplete INTEGER DEFAULT 0;
ALTER TABLE sessions_meta ADD COLUMN last_input_tokens INTEGER;
ALTER TABLE sessions_meta ADD COLUMN last_output_tokens INTEGER;
ALTER TABLE sessions_meta ADD COLUMN last_reasoning_tokens INTEGER;
ALTER TABLE sessions_meta ADD COLUMN working_seconds_msg INTEGER;
ALTER TABLE sessions_meta ADD COLUMN metrics_refreshed_at TEXT;
ALTER TABLE sessions_meta ADD COLUMN pricing_version TEXT;

-- Normalized resume tracking (A5 arch-strategist):
-- `ord` = indice monotono nella chain di resume; PK composta previene dup.
CREATE TABLE IF NOT EXISTS session_conversations (
    session_name TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    ord INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (session_name, conversation_id),
    FOREIGN KEY (session_name) REFERENCES sessions_meta(name) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_session_conv_name ON session_conversations(session_name);
CREATE INDEX IF NOT EXISTS idx_session_conv_id ON session_conversations(conversation_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (87);
COMMIT;
