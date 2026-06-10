-- v1.0.0 - 2026-02-24 - Initial schema for Console PiR

CREATE TABLE IF NOT EXISTS sessions_meta (
    name TEXT PRIMARY KEY,
    display_name TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_active DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ws_tickets (
    ticket TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    session_name TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS token_blacklist (
    jti TEXT PRIMARY KEY,
    blacklisted_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_versions (
    version INTEGER PRIMARY KEY,
    applied_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

INSERT OR IGNORE INTO schema_versions (version) VALUES (1);

CREATE INDEX IF NOT EXISTS idx_ws_tickets_expires ON ws_tickets(expires_at);
CREATE INDEX IF NOT EXISTS idx_blacklist_expires ON token_blacklist(expires_at);
