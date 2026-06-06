-- Rollback migration 142: drop llm_function_config + provider_keys.
-- v1.0.0 - 2026-05-26 - M1 CAPTURE U5

DROP TABLE IF EXISTS llm_function_config;
DROP INDEX IF EXISTS idx_provider_keys_workspace;
DROP TABLE IF EXISTS provider_keys;

DELETE FROM schema_versions WHERE version = 142;
