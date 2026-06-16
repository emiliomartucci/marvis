-- v1.0.0 - 2026-03-02 - Add conversation_id to pull_requests for worktree agent cost tracking
PRAGMA foreign_keys=ON;

ALTER TABLE pull_requests
    ADD COLUMN conversation_id TEXT
    REFERENCES session_costs(conversation_id) ON DELETE SET NULL;

INSERT OR IGNORE INTO schema_versions (version) VALUES (24);
