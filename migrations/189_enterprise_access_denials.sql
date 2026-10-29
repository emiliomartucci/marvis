-- Enterprise-only authorization effect: a person's explicit project denial
-- outranks direct, team and public read grants. Hosted keeps its existing
-- grant semantics until Enterprise strict mode is enabled.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS access_denials (
    workspace_id TEXT NOT NULL,
    identity TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    created_by TEXT NOT NULL,
    PRIMARY KEY (workspace_id, identity, project_slug)
);

CREATE INDEX IF NOT EXISTS idx_access_denials_identity
    ON access_denials(workspace_id, identity);

INSERT OR IGNORE INTO schema_versions(version) VALUES (189);
COMMIT;
