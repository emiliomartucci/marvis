-- Migration 176 is a security floor and is intentionally not reversible in
-- place. Keep schema version and enforcement so older images fail closed.
-- Operational rollback requires a pre-activation backup or a forward-compatible
-- image that understands chained writes.

BEGIN IMMEDIATE;
SELECT enforcement_enabled, activated_at, legacy_root_hash
FROM audit_chain_state WHERE id = 1;
COMMIT;
