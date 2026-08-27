-- Migration 186 rollback: never discard an in-flight lifecycle lease.

BEGIN IMMEDIATE;

CREATE TEMP TABLE v186_active_lease_gate (
    ok INTEGER NOT NULL CHECK (ok = 1)
);
INSERT INTO v186_active_lease_gate(ok)
SELECT CASE WHEN EXISTS (
    SELECT 1 FROM session_operation_leases
     WHERE operation IS NOT NULL
       AND lease_expires_at > strftime('%Y-%m-%dT%H:%M:%fZ','now')
) THEN 0 ELSE 1 END;
DROP TABLE v186_active_lease_gate;

DROP INDEX IF EXISTS idx_session_operation_leases_active;
DROP TABLE session_operation_leases;
DELETE FROM schema_versions WHERE version = 186;

COMMIT;
