-- Down migration: drop the agent-authorship provenance column. SQLite ALTER
-- TABLE DROP COLUMN requires 3.35+; run manually (the migration runner ignores
-- *_down.sql).
ALTER TABLE brain_findings DROP COLUMN authored_by_agent;
