-- Migration 049: Agent role normalization + learnings schema for REM consolidation + access tracking
-- Schema changes handled in Python post-hook (_migration_049 in db.py)
INSERT OR IGNORE INTO schema_versions (version) VALUES (49);
