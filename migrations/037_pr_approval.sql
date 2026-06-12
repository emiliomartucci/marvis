-- v037: Phase 2 — PR approval workflow (four-eyes gate)

ALTER TABLE pull_requests ADD COLUMN approved_by TEXT REFERENCES users(id) ON DELETE SET NULL;
ALTER TABLE pull_requests ADD COLUMN approved_at DATETIME;

-- Colonne provisioning utenti (Fase 3 prep, aggiunte ora per atomicita migration)
ALTER TABLE users ADD COLUMN linux_username TEXT;
ALTER TABLE users ADD COLUMN provisioned_at DATETIME;
ALTER TABLE users ADD COLUMN onboarding_completed INTEGER NOT NULL DEFAULT 0;

CREATE UNIQUE INDEX IF NOT EXISTS idx_users_linux_username
    ON users(linux_username) WHERE linux_username IS NOT NULL;

INSERT OR IGNORE INTO schema_versions (version, applied_at)
VALUES (37, datetime('now'));
