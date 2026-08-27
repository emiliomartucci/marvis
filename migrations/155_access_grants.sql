-- Migration 155 — hosted tenant access grants.
--
-- Sprint 1 hosted-tenant-first U2: identity -> project role + confidential
-- clearance. Absence of a row is default-deny for multi-user tenants; local and
-- static-admin paths remain unrestricted in application code.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS access_grants (
    identity TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'membro', 'member')),
    confidential_clearance INTEGER NOT NULL DEFAULT 0
        CHECK(confidential_clearance IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (identity, project_slug)
);

CREATE INDEX IF NOT EXISTS idx_access_grants_identity
    ON access_grants(identity);

CREATE INDEX IF NOT EXISTS idx_access_grants_project
    ON access_grants(project_slug);

INSERT OR IGNORE INTO schema_versions(version) VALUES (155);

COMMIT;
