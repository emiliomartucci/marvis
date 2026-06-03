-- v051 - 2026-04-02 - Persist launch model, permission preset, and bootstrap message
ALTER TABLE sessions_meta ADD COLUMN launch_model TEXT;
ALTER TABLE sessions_meta ADD COLUMN permission_preset TEXT;
ALTER TABLE sessions_meta ADD COLUMN bootstrap_message TEXT;
