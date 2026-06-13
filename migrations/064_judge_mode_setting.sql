-- Judge mode app_setting: shadow (default), true, false
-- Shadow mode: judge runs but verdict is logged only, proposal always passes
INSERT OR IGNORE INTO app_settings (key, value) VALUES ('judge_mode', 'shadow');
INSERT INTO schema_versions (version) VALUES (64);
