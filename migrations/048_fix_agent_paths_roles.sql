-- Migration 048: Fix agent soul_path, tools_path, identity_path + system_role
-- All UPDATEs in Python hook _fix_agent_paths_and_roles() in db.py.
-- Same pattern as migration 047 (Python hook for FK-safe updates).

INSERT OR IGNORE INTO schema_versions (version) VALUES (48);
