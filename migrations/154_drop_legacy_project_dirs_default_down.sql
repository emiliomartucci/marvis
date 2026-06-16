-- Restore the legacy seeded default removed by 154 (only meaningful for
-- rollback symmetry; INSERT OR IGNORE keeps any user-set value intact).
INSERT OR IGNORE INTO settings (key, value) VALUES (
    'project_dirs',
    '["~/marvis/projects-work", "~/marvis/projects-personal"]'
);
