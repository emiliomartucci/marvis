-- Down 150 — drop super-session delegations.
DROP INDEX IF EXISTS idx_delegations_agent_active;
DROP TABLE IF EXISTS delegations;
