-- v1.0.0 - 2026-04-14 - digest selection app settings defaults

INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_enabled', 'shadow');
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_freeze_hour_utc', '6');
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_admission_threshold', '1.0');
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_overflow_ttl_days', '3');
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('inbox_daily_digest_last_cycle_key', '');

INSERT OR IGNORE INTO schema_versions (version) VALUES (68);
