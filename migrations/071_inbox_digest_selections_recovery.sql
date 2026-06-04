-- v1.0.0 - 2026-04-14 - recover digest selection table on upgraded DBs

INSERT OR IGNORE INTO schema_versions (version) VALUES (71);
