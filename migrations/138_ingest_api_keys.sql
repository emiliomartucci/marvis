-- Migration 138: ingestion API-key credential (single-org).
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U1
--
-- Purpose-built credential for the governed ingestion ingress (POST /api/v1/ingest).
-- NOT an extension of agent_tokens: agent_tokens is load-bearing for the 91-tool
-- MCP server; a separate table reuses the crypto (SHA-256 hash via security.py)
-- while isolating the per-source policy (no blast radius on MCP auth).
--
-- Convergence with agent_tokens (future M7 merge): same hashing helper,
-- same revoke-audit shape (revoked_at/by/reason), same expires_at semantics.
--
-- Rollback: migrations/138_ingest_api_keys_down.sql

CREATE TABLE IF NOT EXISTS ingest_api_keys (
    id                 TEXT PRIMARY KEY,                 -- uuid
    name               TEXT NOT NULL,                    -- human label
    token_hash         TEXT NOT NULL UNIQUE,             -- SHA-256 hex (reuse security.py _hash_token)
    prefix             TEXT NOT NULL,                    -- first chars for display (ing_xxxx...)
    project_scope      TEXT NOT NULL DEFAULT '[]',       -- JSON array of slugs (default-deny)
    ingest_policy      TEXT NOT NULL DEFAULT 'open'
                         CHECK(ingest_policy IN ('open', 'trusted')),
    default_source     TEXT,                             -- label applied when payload omits source
    rate_limit_per_min INTEGER NOT NULL DEFAULT 60,
    daily_quota        INTEGER NOT NULL DEFAULT 1000,    -- durable abuse backstop (non-NULL by design)
    workspace_id       TEXT NOT NULL DEFAULT 'ws_default',
    created_by         TEXT,                             -- user slug who minted it
    created_at         TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at         TEXT,                             -- NULL = no expiry
    last_used_at       TEXT,
    is_active          INTEGER NOT NULL DEFAULT 1,
    revoked_at         TEXT,
    revoked_by         TEXT,
    revoke_reason      TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_api_keys_hash ON ingest_api_keys(token_hash);
CREATE INDEX IF NOT EXISTS idx_ingest_api_keys_active ON ingest_api_keys(is_active, workspace_id);

INSERT OR IGNORE INTO schema_versions(version) VALUES (138);
