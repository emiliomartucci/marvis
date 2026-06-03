-- v1.0.0 - 2026-04-14 - recover digest ranking input columns on upgraded DBs

INSERT OR IGNORE INTO schema_versions (version) VALUES (70);
