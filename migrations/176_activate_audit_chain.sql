-- Migration 176: forward-only activation marker for the v175 audit chain.
--
-- The guarded Python post-hook freezes the complete legacy prefix and switches
-- enforcement on in one BEGIN IMMEDIATE transaction. Recording the version
-- first makes an interrupted post-hook recoverable on the next startup.

BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (176);
COMMIT;
