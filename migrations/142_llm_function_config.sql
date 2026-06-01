-- Migration 142: per-function LLM config + encrypted provider keys (single-org).
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U5 (BYOK)
--
-- Seeds the M7.6 BYOK key manager: provider keys encrypted at rest (crypto.py
-- org cipher, BYOK_FERNET_SECRET) + per-function provider/model selection.
-- The schema (table/column names + the function_name enum classify|embedding|brain)
-- is a PREREQUISITE that M4 (reflect/brain) and M8.2 (wizard) assume as-designed.
--
-- Rollback: migrations/142_llm_function_config_down.sql

CREATE TABLE IF NOT EXISTS provider_keys (
    id             TEXT PRIMARY KEY,                    -- uuid
    provider       TEXT NOT NULL CHECK(provider IN (
                       'openai', 'anthropic', 'ollama',
                       'openai_compatible', 'mac_gateway'
                   )),
    label          TEXT,
    key_ciphertext TEXT,                                -- versioned 'v1:' ciphertext; NULL for keyless (ollama / mac_gateway)
    base_url       TEXT,                                -- ollama / openai_compatible / mac_gateway
    workspace_id   TEXT NOT NULL DEFAULT 'ws_default',
    created_at     TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_provider_keys_workspace ON provider_keys(workspace_id);

CREATE TABLE IF NOT EXISTS llm_function_config (
    function_name   TEXT NOT NULL CHECK(function_name IN ('classify', 'embedding', 'brain')),
    provider_key_id TEXT REFERENCES provider_keys(id) ON DELETE SET NULL,
    model           TEXT,
    enabled         INTEGER NOT NULL DEFAULT 0,
    workspace_id    TEXT NOT NULL DEFAULT 'ws_default',
    updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (function_name, workspace_id)
);

INSERT OR IGNORE INTO schema_versions(version) VALUES (142);
