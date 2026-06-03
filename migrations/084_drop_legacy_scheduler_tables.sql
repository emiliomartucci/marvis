-- Migration 084: Drop legacy scheduler tables (Phase 2 deprecation)
--
-- Context:
--   Phase 1 (task 8958bbf5, PR a8fdef99, SHA fe4a264) has removed the legacy
--   agent-scheduler code from api/ and console/ (merged + deployed). This
--   migration removes the residual DB schema for that scheduling + metrics system.
--
-- Scope:
--   Drop tables: agents, agent_schedules, agent_runs, agent_daily_metrics, agent_sync_state
--   These were populated exclusively by the legacy cron/orchestrator and by migrations
--   012/018/022/026/044/047/048/049. No live API router writes here after Phase 1.
--
-- Preserved (NOT dropped):
--   - agent_actions (2479 rows): used by api/routers/agent.py for session audit log
--   - agent_tokens  (5 rows):    used by api/routers/agent_tokens.py for Bearer auth
--   - users:                     scheduler_agent_id / scheduler_job_id columns do NOT exist here
--                                 (they live on agents / agent_schedules and are dropped
--                                 together with the parent table)
--
-- Irreversible: rows are lost intentionally. Pre-migration snapshot is taken automatically
-- by api/db.py run_migrations (console.db.backup-v{N}, keep-3 rotation).
--
-- Pre-migration counts (server console.db at 2026-04-22):
--   agents:              6
--   agent_schedules:     3
--   agent_runs:          8
--   agent_daily_metrics: 53
--   agent_sync_state:    0
--
-- Rollback: 084_drop_legacy_scheduler_tables_down.sql recreates the schema only (no data).
--            For data recovery, restore from console.db.backup-v83.

PRAGMA foreign_keys = OFF;

-- Drop in dependency order (children before parents)
DROP INDEX IF EXISTS idx_agent_runs_status;
DROP INDEX IF EXISTS idx_agent_runs_agent_started;
DROP INDEX IF EXISTS idx_agent_runs_agent_id;
DROP INDEX IF EXISTS idx_cost_runs_agent;
DROP TABLE IF EXISTS agent_runs;

DROP INDEX IF EXISTS idx_agent_metrics_agent;
DROP INDEX IF EXISTS idx_agent_metrics_ws_date;
DROP TABLE IF EXISTS agent_daily_metrics;

DROP INDEX IF EXISTS idx_agent_schedules_agent_id;
DROP TABLE IF EXISTS agent_schedules;

DROP TABLE IF EXISTS agent_sync_state;

DROP INDEX IF EXISTS idx_agents_active;
DROP INDEX IF EXISTS idx_agents_project_slug;
DROP INDEX IF EXISTS idx_agents_user_id;
DROP TABLE IF EXISTS agents;

PRAGMA foreign_keys = ON;

INSERT OR IGNORE INTO schema_versions (version) VALUES (84);
