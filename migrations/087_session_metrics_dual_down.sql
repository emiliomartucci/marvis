-- Down migration for 087
-- SQLite 3.35+ native ALTER TABLE DROP COLUMN (server has 3.45+).

BEGIN IMMEDIATE;

DROP TABLE IF EXISTS session_conversations;

ALTER TABLE sessions_meta DROP COLUMN last_context_pct_real;
ALTER TABLE sessions_meta DROP COLUMN last_context_pct_scaled;
ALTER TABLE sessions_meta DROP COLUMN last_cost_conversation_usd;
ALTER TABLE sessions_meta DROP COLUMN last_cost_session_usd;
ALTER TABLE sessions_meta DROP COLUMN last_cost_session_incomplete;
ALTER TABLE sessions_meta DROP COLUMN last_input_tokens;
ALTER TABLE sessions_meta DROP COLUMN last_output_tokens;
ALTER TABLE sessions_meta DROP COLUMN last_reasoning_tokens;
ALTER TABLE sessions_meta DROP COLUMN working_seconds_msg;
ALTER TABLE sessions_meta DROP COLUMN metrics_refreshed_at;
ALTER TABLE sessions_meta DROP COLUMN pricing_version;

DELETE FROM schema_versions WHERE version = 87;
COMMIT;
