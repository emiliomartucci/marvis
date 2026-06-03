-- Down migration 145 — drop audit_log append-only triggers.
DROP TRIGGER IF EXISTS audit_log_no_delete;
DROP TRIGGER IF EXISTS audit_log_no_update;
DELETE FROM schema_versions WHERE version = 145;
