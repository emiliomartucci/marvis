-- Down migration for 089
-- SQLite 3.35+ native ALTER TABLE DROP COLUMN (server has 3.45+).

BEGIN IMMEDIATE;

ALTER TABLE sessions_meta DROP COLUMN last_cost_conversation_equivalent_usd;
ALTER TABLE sessions_meta DROP COLUMN last_cost_session_equivalent_usd;
ALTER TABLE sessions_meta DROP COLUMN last_cost_equivalent_pricing_version;

DELETE FROM schema_versions WHERE version = 89;
COMMIT;
