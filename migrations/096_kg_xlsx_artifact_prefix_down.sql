-- Down migration 096 — rollback XLSX node prefix (code-level no-op).
--
-- Full rollback requires reverting the NODE_ID_PATTERN/NODE_ID_RE changes and
-- optionally cleaning rows with `id LIKE 'xlsx:%'`. This down migration only
-- removes the schema_versions marker, preserving indexed data conservatively.

BEGIN IMMEDIATE;

DELETE FROM schema_versions WHERE version = 96;

COMMIT;
