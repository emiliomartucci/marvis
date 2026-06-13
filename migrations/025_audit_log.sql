-- 024_audit_log.sql
-- Audit trail for human privileged operations (merge PR, approve/complete tasks, delete resources)
-- 2026-03-03
CREATE TABLE IF NOT EXISTS audit_log (
    id            TEXT PRIMARY KEY DEFAULT (lower(hex(randomblob(16)))),
    timestamp     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    action        TEXT NOT NULL,
    user          TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id   TEXT NOT NULL,
    details_json  TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp     ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_log_user          ON audit_log(user);
CREATE INDEX IF NOT EXISTS idx_audit_log_resource_type ON audit_log(resource_type, resource_id);

INSERT OR IGNORE INTO schema_versions (version) VALUES (25);
