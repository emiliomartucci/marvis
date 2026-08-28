-- Migration 178 is a forward-only authentication boundary.  Downgrade must
-- not remove principal binding, expiry, rotation, or revocation metadata.

BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (178);
COMMIT;
