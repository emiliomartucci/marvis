-- Shareable file links with expiring tokens
INSERT INTO schema_versions (version) VALUES (35);

CREATE TABLE IF NOT EXISTS shared_links (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    access_count INTEGER DEFAULT 0,
    last_accessed_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_shared_links_token ON shared_links(token);
CREATE INDEX IF NOT EXISTS idx_shared_links_expires ON shared_links(expires_at);
