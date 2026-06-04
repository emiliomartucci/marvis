-- Migration 042: SSO/OIDC provider configuration
-- Supports WorkOS AuthKit + future custom OIDC/SAML providers

CREATE TABLE IF NOT EXISTS oidc_providers (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    provider_type TEXT NOT NULL CHECK(provider_type IN ('workos','custom_oidc','saml')),
    issuer_url TEXT,
    client_id TEXT NOT NULL,
    client_secret_encrypted TEXT NOT NULL,
    allowed_email_domains TEXT DEFAULT '[]',  -- JSON array of allowed domains
    claims_mapping TEXT DEFAULT '{}',  -- JSON: OIDC claim -> MarvisX field
    enabled INTEGER DEFAULT 1,
    created_at DATETIME DEFAULT (datetime('now','utc')),
    UNIQUE(workspace_id, slug)
);

-- Link users to external identity providers
ALTER TABLE users ADD COLUMN external_id TEXT;  -- OIDC sub claim
ALTER TABLE users ADD COLUMN auth_provider TEXT DEFAULT 'local';  -- local|workos|oidc
CREATE INDEX IF NOT EXISTS idx_users_external ON users(auth_provider, external_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (42);
