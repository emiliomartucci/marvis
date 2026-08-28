-- Migration 160 — project_teams role/clearance (RBAC F2, plan d33292f0).
--
-- A team assignment now confers a grant on the project: role is what the
-- team members get (member|viewer), clearance is the ceiling the team can
-- confer (public|internal). Teams NEVER confer confidential clearance —
-- owner-confidential is a separate per-file layer (F4).
-- The legacy is_public column is NOT consumed by the multi-user predicate.

BEGIN IMMEDIATE;

ALTER TABLE project_teams ADD COLUMN role TEXT NOT NULL DEFAULT 'member'
    CHECK(role IN ('member', 'viewer'));
ALTER TABLE project_teams ADD COLUMN clearance TEXT NOT NULL DEFAULT 'internal'
    CHECK(clearance IN ('public', 'internal'));

INSERT OR IGNORE INTO schema_versions(version) VALUES (160);

COMMIT;
