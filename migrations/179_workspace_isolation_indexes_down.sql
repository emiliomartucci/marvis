-- Migration 179 is a security boundary. Keep its columns, triggers, indexes,
-- and version marker so an older image fails its forward-only schema guard
-- instead of silently returning to cross-workspace grant evaluation.
BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (179);
COMMIT;
