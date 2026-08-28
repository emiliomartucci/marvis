-- Migration 177 is a forward-only authorization boundary. Downgrade must not
-- remove workspace scoping or make legacy unscoped grants live again.

BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (177);
COMMIT;
