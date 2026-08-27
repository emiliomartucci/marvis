-- Migration 175 establishes a forward-only security floor once activated.
--
-- Audit rows, chain columns, heads, state, guards, and the schema version remain
-- intact. In-place downgrade would either permit chainless writes or make an old
-- image falsely appear compatible. Restore a pre-activation backup or use a
-- forward-compatible rollback image instead.

BEGIN IMMEDIATE;

SELECT enforcement_enabled, activated_at, legacy_root_hash
FROM audit_chain_state WHERE id = 1;

COMMIT;
