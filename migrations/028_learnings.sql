-- Learnings DB: consultable knowledge base to prevent recurring errors
-- deploy gap 3x, migration collision 3x, subtree push 3x → never again

CREATE TABLE IF NOT EXISTS learnings (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    category TEXT NOT NULL,          -- 'deploy', 'migration', 'auth', 'testing', 'architecture', 'security', 'performance'
    description TEXT NOT NULL,       -- what happened + how it was resolved
    tags TEXT DEFAULT '[]',          -- JSON array of tags
    module TEXT,                     -- module involved (e.g. 'api/security.py', 'console', 'scripts')
    severity TEXT DEFAULT 'medium',  -- 'low', 'medium', 'high', 'critical'
    frequency INTEGER DEFAULT 1,    -- how many times it occurred
    last_occurrence TEXT,            -- ISO datetime of last occurrence
    prevention TEXT,                 -- what to do to prevent it
    session INTEGER,                 -- session number where documented
    project TEXT,                    -- project slug
    created_at TEXT NOT NULL DEFAULT (datetime('now','utc')),
    updated_at TEXT
);

CREATE INDEX idx_learnings_category ON learnings(category);
CREATE INDEX idx_learnings_project ON learnings(project);
CREATE INDEX idx_learnings_tags ON learnings(tags);
