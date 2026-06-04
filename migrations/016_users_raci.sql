-- 016_users_raci.sql
-- v1.0.0 - 2026-02-28 - Users anagrafica, RACI, events outbox, owner_id, user_integrations
-- NOTE: project references are slugs (TEXT, no FK by design — projects live in filesystem/yaml).
-- NOTE: tasks.assigned_to renamed to owner_id; no REFERENCES constraint yet for graceful data migration.
--       Python post-hook (db.py _seed_users_and_migrate_owner) resolves slug values to users.id.

-- === Tabella users (anagrafica + auth placeholder per Fase B) ===
CREATE TABLE IF NOT EXISTS users (
    id                    TEXT PRIMARY KEY,           -- 'usr_emilio', 'usr_marvisx'
    slug                  TEXT UNIQUE NOT NULL,       -- 'emilio', 'marvisx-agent'
    display_name          TEXT NOT NULL,
    type                  TEXT NOT NULL DEFAULT 'human'
                          CHECK(type IN ('human', 'agent')),
    email                 TEXT,
    password_hash         TEXT,                       -- bcrypt, human only (attivo in Fase B)
    bearer_token_hash     TEXT,                       -- SHA-256, agent only (attivo in Fase B)
    last_used_at          TEXT,
    avatar_color          TEXT NOT NULL DEFAULT '#6366f1',
    system_role           TEXT NOT NULL DEFAULT 'viewer'
                          CHECK(system_role IN ('admin', 'operator', 'viewer', 'super_admin')),
    notification_channels TEXT NOT NULL DEFAULT '[]', -- JSON: ['telegram','email']
    telegram_chat_id      TEXT,
    deleted_at            TEXT,                       -- soft delete (NULL = attivo)
    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_users_slug   ON users(slug);
CREATE INDEX IF NOT EXISTS idx_users_type   ON users(type);
CREATE INDEX IF NOT EXISTS idx_users_active ON users(deleted_at) WHERE deleted_at IS NULL;

-- === Tabella project_raci ===
CREATE TABLE IF NOT EXISTS project_raci (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    project    TEXT NOT NULL,
    user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL
               CHECK(role IN ('responsible', 'accountable', 'consulted', 'informed')),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(project, user_id, role)
);

-- Standard RACI: esattamente 1 Responsible e 1 Accountable per progetto
-- Consulted e Informed: multipli ok
CREATE UNIQUE INDEX IF NOT EXISTS idx_raci_one_responsible
    ON project_raci(project) WHERE role = 'responsible';
CREATE UNIQUE INDEX IF NOT EXISTS idx_raci_one_accountable
    ON project_raci(project) WHERE role = 'accountable';

CREATE INDEX IF NOT EXISTS idx_raci_project ON project_raci(project);
CREATE INDEX IF NOT EXISTS idx_raci_user    ON project_raci(user_id);

-- === Audit trail RACI (append-only — MAI UPDATE o DELETE) ===
CREATE TABLE IF NOT EXISTS project_raci_history (
    id         TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    project    TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    role       TEXT NOT NULL,
    action     TEXT NOT NULL CHECK(action IN ('assign', 'revoke', 'restore')),
    changed_by TEXT NOT NULL REFERENCES users(id),
    changed_at TEXT NOT NULL DEFAULT (datetime('now')),
    reason     TEXT
);

CREATE INDEX IF NOT EXISTS idx_raci_history_project
    ON project_raci_history(project, changed_at DESC);

-- === user_integrations (n8n credential references, logica attiva in Fase 4) ===
-- n8n non espone i secret via API: MarvisX salva solo external_ref_id (credential UUID)
CREATE TABLE IF NOT EXISTS user_integrations (
    id               TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    user_id          TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    integration_type TEXT NOT NULL,   -- 'n8n_credential', 'github', ecc.
    external_ref_id  TEXT NOT NULL,   -- n8n credential UUID (mai il valore segreto)
    credential_type  TEXT,            -- n8n type: 'gmailOAuth2', 'githubApi', ecc.
    display_name     TEXT,
    n8n_workflow_id  TEXT,
    is_active        INTEGER NOT NULL DEFAULT 1,
    last_verified    TEXT,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at       TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(user_id, integration_type, external_ref_id)
);

CREATE INDEX IF NOT EXISTS idx_ui_user
    ON user_integrations(user_id) WHERE is_active = 1;

-- === events (transactional outbox — prerequisito Fase 3 notifiche + Fase 4 n8n) ===
-- dispatched_at IS NULL = pending (non ancora processato dal consumer)
-- Un background worker legge WHERE dispatched_at IS NULL, invia, imposta dispatched_at
CREATE TABLE IF NOT EXISTS events (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(8)))),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    event_type    TEXT NOT NULL,           -- 'task.status_changed', 'raci.updated', 'pr.merged'
    project       TEXT,
    actor_id      TEXT REFERENCES users(id) ON DELETE SET NULL,
    target_type   TEXT CHECK(target_type IN ('task', 'project', 'pr', 'raci', 'comment')),
    target_id     TEXT,
    payload       TEXT NOT NULL DEFAULT '{}',
    dispatched_at TEXT DEFAULT NULL        -- NULL = pending, timestamp = processato
);

CREATE INDEX IF NOT EXISTS idx_events_pending
    ON events(created_at) WHERE dispatched_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_events_project_created
    ON events(project, created_at DESC) WHERE project IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_events_type_created
    ON events(event_type, created_at DESC);

-- === Rinomina tasks.assigned_to → tasks.owner_id ===
-- Nessuna FK REFERENCES aggiunta ora: i valori esistenti (es. "emilio") vengono
-- migrati dal Python hook post-migration (_seed_users_and_migrate_owner in db.py).
-- Aggiungere REFERENCES users(id) solo dopo che tutti i valori sono stati puliti.
ALTER TABLE tasks RENAME COLUMN assigned_to TO owner_id;

INSERT OR IGNORE INTO schema_versions (version) VALUES (16);
