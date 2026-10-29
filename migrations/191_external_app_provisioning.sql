-- Enterprise provisioning of Marvis users into external applications
-- (AI gateway keys, Chatwoot accounts). Hosted does not read these tables;
-- Enterprise Console writes them. Explicit rows only: deny by default.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS external_app_entitlements (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    app TEXT NOT NULL,
    subject_type TEXT NOT NULL CHECK (subject_type IN ('user', 'team')),
    subject_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('enabled', 'denied')),
    elevated INTEGER NOT NULL DEFAULT 0 CHECK (elevated IN (0, 1)),
    params_json TEXT NOT NULL DEFAULT '{}',
    updated_by TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (workspace_id, app, subject_type, subject_id),
    -- Team rows only enable; a denial or an elevated role is always explicit per user.
    CHECK (subject_type = 'user' OR (state = 'enabled' AND elevated = 0))
);

CREATE TABLE IF NOT EXISTS external_app_accounts (
    workspace_id TEXT NOT NULL,
    app TEXT NOT NULL,
    user_id TEXT NOT NULL,
    remote_id TEXT,
    request_name TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'active', 'revoked', 'uncertain')),
    desired_hash TEXT,
    last_error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (workspace_id, app, user_id)
);

CREATE TABLE IF NOT EXISTS external_app_sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    app TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    outcome TEXT,
    counts_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_external_app_sync_runs_recent
    ON external_app_sync_runs (workspace_id, app, started_at);

INSERT OR IGNORE INTO schema_versions(version) VALUES (191);
COMMIT;
