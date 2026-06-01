-- v1.0.0 - 2026-04-17 - Rollback migration 082 (kg_pins)
-- For dev/test only. Production rollback = restore backup (api/db.py keeps last 3).
PRAGMA foreign_keys=OFF;
BEGIN IMMEDIATE;

DROP TRIGGER IF EXISTS trg_kg_pins_cleanup;
DROP INDEX IF EXISTS idx_kg_pins_node;
DROP INDEX IF EXISTS idx_kg_pins_ws_user;
DROP TABLE IF EXISTS kg_pins;

DELETE FROM schema_versions WHERE version = 82;

COMMIT;
PRAGMA foreign_keys=ON;
