-- Migration 178: bind every newly issued agent token to one principal,
-- workspace, bounded lifetime, rotation family, and revocation lifecycle.
--
-- SQLite cannot add guarded columns in pure SQL.  The idempotent Python
-- post-hook in core/api/db.py owns the additive DDL and legacy backfill.

BEGIN IMMEDIATE;
INSERT OR IGNORE INTO schema_versions (version) VALUES (178);
COMMIT;
