-- Settings key-value store
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- Default project directories
INSERT OR IGNORE INTO settings (key, value) VALUES (
    'project_dirs',
    '["~/marvis/projects-work", "~/marvis/projects-personal"]'
);
