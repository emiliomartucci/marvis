-- Migration 057: backfill sessions_meta.theme_mode for databases already past schema version 52
-- Column added via Python hook in db.py (_add_session_theme_mode_column) AFTER this SQL runs.

INSERT OR IGNORE INTO schema_versions (version) VALUES (57);
