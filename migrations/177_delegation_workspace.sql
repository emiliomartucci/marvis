-- Migration 177: bind super-session delegations to one explicit workspace.
--
-- The guarded Python post-hook adds the column and composite lookup index.
-- Existing rows deliberately remain NULL and therefore fail closed: no legacy
-- grant is silently authorized in an inferred tenant.

BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (177);
COMMIT;
