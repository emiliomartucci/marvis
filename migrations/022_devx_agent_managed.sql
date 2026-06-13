-- 022_devx_agent_managed.sql
-- Adds agent_managed toggle to sessions_meta for DevX orchestration
ALTER TABLE sessions_meta ADD COLUMN agent_managed INTEGER NOT NULL DEFAULT 0
    CHECK (agent_managed IN (0, 1));

-- Partial index: solo sessioni agent_managed=1 sono interrogate dal Session Manager
CREATE INDEX IF NOT EXISTS idx_sessions_agent_managed
    ON sessions_meta(agent_managed)
    WHERE agent_managed = 1;

INSERT OR IGNORE INTO schema_versions (version) VALUES (22);
