-- Migration 156 — rich tenant access grants.
--
-- Sprint 1 U3: role admin/member/viewer, clearance public/internal/confidential,
-- and scope all/project:<slug>/file:<prefix>. Keeps the legacy
-- confidential_clearance column as a compatibility mirror for already-deployed
-- read paths and old tests.

BEGIN IMMEDIATE;

CREATE TABLE IF NOT EXISTS access_grants_rich_new (
    identity TEXT NOT NULL,
    project_slug TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin', 'member', 'viewer', 'membro')),
    confidential_clearance INTEGER NOT NULL DEFAULT 0
        CHECK(confidential_clearance IN (0, 1)),
    clearance TEXT NOT NULL DEFAULT 'internal'
        CHECK(clearance IN ('public', 'internal', 'confidential')),
    scope TEXT NOT NULL DEFAULT 'all',
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (identity, project_slug)
);

INSERT OR REPLACE INTO access_grants_rich_new (
    identity, project_slug, role, confidential_clearance, clearance, scope, created_at, updated_at
)
SELECT
    identity,
    project_slug,
    CASE role WHEN 'membro' THEN 'member' ELSE role END,
    confidential_clearance,
    CASE confidential_clearance WHEN 1 THEN 'confidential' ELSE 'internal' END,
    'project:' || project_slug,
    created_at,
    updated_at
FROM access_grants;

DROP TABLE access_grants;
ALTER TABLE access_grants_rich_new RENAME TO access_grants;

CREATE INDEX IF NOT EXISTS idx_access_grants_identity
    ON access_grants(identity);

CREATE INDEX IF NOT EXISTS idx_access_grants_project
    ON access_grants(project_slug);

INSERT OR IGNORE INTO schema_versions(version) VALUES (156);

COMMIT;
